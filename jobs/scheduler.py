"""
Daily incremental data-sync scheduler (asyncio background task; scheduler lives as
long as the backend does).

Schedule rules:
- Runs on every trading day at 17:00 Asia/Shanghai.
- Incremental: last 5 trading days of quotes (daily/daily_basic/adj_factor) + current
  week's financials
- Native 60F gap repair, coverage audit and atomic trend snapshot publication
- Idempotent overlap upserts; failures retry every 30 minutes, up to three times

Design:
- asyncio task attached to the FastAPI lifespan
- Pure standard library, no external deps (no APScheduler, avoiding network-install
  failures)
- Manual trigger endpoint supported: /admin/sync-today
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from core.config import get_config as _cfg
from core.logger import get_logger
from core.observability import observed
from tools.kline.calendar import is_trading_day

logger = get_logger(__name__)

# Configurable local-market clock, independent of the host operating-system zone.
SHANGHAI = ZoneInfo("Asia/Shanghai")
SYNC_HOUR = int(_cfg().get("schedule.sync_hour", 17))
SYNC_MINUTE = int(_cfg().get("schedule.sync_minute", 0))
if not 0 <= SYNC_HOUR <= 23 or not 0 <= SYNC_MINUTE <= 59:
    raise ValueError("schedule.sync_hour/sync_minute超出合法范围")
# Financial-report announcement lookback (calendar days; covers weekend/holiday
# announcement pile-ups)
FIN_BACKFILL_DAYS = 10


def _market_time(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(SHANGHAI)
    return (
        now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    )


@observed("financial_sync", "财务报表增量同步")
def _run_financial_incremental(days: int) -> dict:
    """Thread-pool entry: incremental financial-statement sync (blocking)."""
    from tools.market.sync_financial import run_financial_incremental

    return run_financial_incremental(days=days)


@observed("dividend_sync", "分红送转增量同步")
def _run_dividend_daily(days: int) -> int:
    """Thread-pool entry: per-day incremental dividends/bonus shares
    (3 channels x days days = 3*days calls)."""
    from tools.market.sync_financial import run_dividend_daily

    return run_dividend_daily(days=days)


@observed("disclosure_sync", "财报披露日期同步")
def _run_disclosure_daily(days: int) -> int:
    """Thread-pool entry: per-day incremental report disclosure dates
    (2 channels x days days)."""
    from tools.market.sync_financial import run_disclosure_daily

    return run_disclosure_daily(days=days)


class DailySyncScheduler:
    """Daily incremental-sync scheduler."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._manual_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.last_run: Optional[datetime] = None
        self.last_result: Optional[dict] = None
        self._run_lock = asyncio.Lock()
        self._retry_at: Optional[datetime] = None
        self._retry_count = 0
        self._retry_target: Optional[date] = None

    def _next_run_time(self, now: Optional[datetime] = None) -> datetime:
        """Next trading-day slot in Shanghai time, including weekends/holidays."""
        now = _market_time(now)
        target = now.replace(
            hour=SYNC_HOUR, minute=SYNC_MINUTE, second=0, microsecond=0
        )
        if now >= target:
            target += timedelta(days=1)
        while not is_trading_day(target.date()):
            target += timedelta(days=1)
        if self._retry_at is not None:
            return min(target, self._retry_at)
        return target

    def _seconds_until(self, target: datetime) -> float:
        return max(0.0, (_market_time(target) - _market_time()).total_seconds())

    async def _run_sync(
        self,
        include_financial: bool = True,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> dict:
        """Serialize scheduled/manual runs; IO stays off the HTTP event loop."""
        async with self._run_lock:
            return await self._run_sync_locked(include_financial, start, end)

    def _record_retry(self, end, result):
        if self._retry_target != end:
            self._retry_target, self._retry_count = end, 0
        self._retry_at = None
        if result.get("status") != "complete":
            self._retry_count += 1
            if self._retry_count <= 3:
                self._retry_at = _market_time() + timedelta(minutes=30)
                logger.warning("行情未完成，30分钟后第%s次重试", self._retry_count)
        else:
            self._retry_count = 0

    async def _run_sync_locked(self, include_financial=True, start=None, end=None):
        from jobs.refresh_market import refresh

        end = end or self._expected_latest_trade_date()
        if end is None:
            raise ValueError("无法确定已完成交易日")
        try:
            result = await asyncio.to_thread(refresh, end, start=start)
        except Exception as error:
            logger.exception("行情更新异常")
            result = {"status": "error", "error": str(error)}
        self._record_retry(end, result)

        # On an incomplete market refresh, return to the retry loop immediately.
        # Financial backfills must not consume the 30-minute retry window.
        if include_financial and result.get("status") == "complete":
            try:
                fin_result = await asyncio.to_thread(
                    _run_financial_incremental, FIN_BACKFILL_DAYS
                )
                if isinstance(result, dict):
                    result["financial"] = fin_result
            except Exception as e:
                logger.error(f"财报增量同步异常: {e}")
                if isinstance(result, dict):
                    result["financial"] = {"error": str(e)}

            # Dividend/bonus-share increment: per-day batch over three channels
            # (ann_date/imp_ann_date/ex_date), 6 calls per day
            try:
                div_rows = await asyncio.to_thread(_run_dividend_daily, 2)
                if isinstance(result, dict):
                    result["dividend"] = div_rows
            except Exception as e:
                logger.error(f"分红增量同步异常: {e}")
                if isinstance(result, dict):
                    result["dividend"] = {"error": str(e)}

            # Disclosure-date increment: per-day over two channels (ann_date =
            # planned announcement/changes, actual_date = actual disclosure)
            try:
                dis_rows = await asyncio.to_thread(_run_disclosure_daily, 2)
                if isinstance(result, dict):
                    result["disclosure_date"] = dis_rows
            except Exception as e:
                logger.error(f"披露日期增量同步异常: {e}")
                if isinstance(result, dict):
                    result["disclosure_date"] = {"error": str(e)}

        # A slow startup catch-up or financial sync may cross today's slot.
        # Catch the newly due session now instead of sleeping until tomorrow.
        due = self._expected_latest_trade_date()
        if due is not None and due > end:
            self._retry_at = _market_time()
            self._retry_target, self._retry_count = due, 0
        self.last_run = _market_time()
        self.last_result = result
        logger.info(f"每日增量同步完成: {result}")
        return result

    @staticmethod
    def _expected_latest_trade_date(now: Optional[datetime] = None) -> Optional[date]:
        """
        Compute the latest trading date whose data 'should already be ingested':
        - At/after the configured market slot on a trading day -> today
        - Otherwise                     -> previous trading day
        """
        from tools.market.refresh_clock import expected_date

        return expected_date(_market_time(now), SYNC_HOUR, SYNC_MINUTE)

    async def _catchup_if_missed(self):
        """Check quotes, minute coverage and published snapshot after a restart."""
        expected = self._expected_latest_trade_date()
        if expected is None:
            logger.warning("无法计算应有最新交易日, 跳过启动自检")
            return

        try:
            from jobs.refresh_market import needs_refresh

            missing = await asyncio.to_thread(needs_refresh, expected)
        except Exception as error:
            logger.exception("启动行情自检失败")
            self._record_retry(expected, {"status": "error", "error": str(error)})
            return
        if not missing:
            logger.info("启动自检: 日K/60F/快照已完成至 %s", expected)
            return
        logger.info("启动自检: 补齐日K/60F/快照至 %s", expected)
        await self._run_sync(include_financial=False, end=expected)

    async def _loop(self):
        """Scheduling loop."""
        logger.info(f"增量同步调度器已启动 (每日 {SYNC_HOUR}:{SYNC_MINUTE:02d})")

        # Check all market stages; a fresh daily MAX cannot hide stale minutes.
        await self._catchup_if_missed()

        while not self._stop_event.is_set():
            next_run = self._next_run_time()
            wait = self._seconds_until(next_run)
            logger.info(f"下次增量同步: {next_run} (等待 {wait:.0f}s)")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                try:
                    retry = self._retry_at is not None and next_run == self._retry_at
                    self._retry_at = None
                    if retry:
                        # A concurrent CLI may have finished while we waited.
                        from jobs.refresh_market import needs_refresh

                        expected = self._expected_latest_trade_date()
                        if expected and await asyncio.to_thread(
                            needs_refresh, expected
                        ):
                            await self._run_sync(include_financial=False)
                    else:
                        await self._run_sync()
                except Exception:
                    logger.exception("定时增量同步失败；保留调度任务")
                    self._record_retry(
                        self._expected_latest_trade_date(), {"status": "error"}
                    )

    def status(self):
        return {
            "running": self._task is not None and not self._task.done(),
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_result": self.last_result,
            "next_run": self._next_run_time().isoformat(),
            "time_zone": "Asia/Shanghai",
            "updating": self._run_lock.locked(),
            "retry_count": self._retry_count,
        }

    def start(self):
        """Start the scheduler task."""
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop())
            logger.info("每日增量调度器已启动")

    async def stop(self):
        """Stop the scheduler task."""
        self._stop_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("每日增量调度器已停止")

    async def trigger_now(self) -> dict:
        """Queue a background job; the admin HTTP request never fetches minutes."""
        if self._run_lock.locked() or (
            self._manual_task and not self._manual_task.done()
        ):
            return {"status": "busy"}
        self._manual_task = asyncio.create_task(self._run_sync())
        return {"status": "queued"}


# Singleton
_scheduler: Optional[DailySyncScheduler] = None


def get_scheduler() -> DailySyncScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DailySyncScheduler()
    return _scheduler


def get_digest_scheduler():
    """Lifecycle composition entry; mail runs independently of quote/finance IO."""
    from jobs.market_digest_scheduler import get_digest_scheduler as get_digest

    return get_digest()


def get_intraday_scheduler():
    """Lifecycle composition entry for the independent closed-60F observer."""
    from jobs.intraday_scheduler import get_intraday_scheduler as get_intraday

    return get_intraday()
