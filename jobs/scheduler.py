"""
Daily incremental data-sync scheduler (asyncio background task; scheduler lives as
long as the backend does).

Schedule rules:
- Runs on every trading day at 21:00 (Tushare ingests the day's data at 15:00-16:00;
  pulling at 21:00 is the safe choice)
- Incremental: last 5 trading days of quotes (daily/daily_basic/adj_factor) + current
  week's financials
- Idempotent: safe to re-run (checkpoint resume + upsert)

Design:
- asyncio task attached to the FastAPI lifespan
- Pure standard library, no external deps (no APScheduler, avoiding network-install
  failures)
- Manual trigger endpoint supported: /admin/sync-today
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from typing import Optional

from core.logger import get_logger
from tools.kline.calendar import is_trading_day

logger = get_logger(__name__)

# Schedule: 21:00 on every trading day (Tushare ingests the day's data at 15-16,
# so 21:00 is safe)
# Can be overridden via schedule.sync_hour in config.yaml
from core.config import get_config as _cfg

SYNC_HOUR = int(_cfg().get("schedule.sync_hour", 21))
SYNC_MINUTE = int(_cfg().get("schedule.sync_minute", 0))
# Incremental lookback window (trading days)
BACKFILL_DAYS = 5
# Financial-report announcement lookback (calendar days; covers weekend/holiday
# announcement pile-ups)
FIN_BACKFILL_DAYS = 10


def _run_financial_incremental(days: int) -> dict:
    """Thread-pool entry: incremental financial-statement sync (blocking)."""
    from tools.market.sync_financial import run_financial_incremental
    return run_financial_incremental(days=days)


def _run_dividend_daily(days: int) -> int:
    """Thread-pool entry: per-day incremental dividends/bonus shares
    (3 channels x days days = 3*days calls)."""
    from tools.market.sync_financial import run_dividend_daily
    return run_dividend_daily(days=days)


def _run_disclosure_daily(days: int) -> int:
    """Thread-pool entry: per-day incremental report disclosure dates
    (2 channels x days days)."""
    from tools.market.sync_financial import run_disclosure_daily
    return run_disclosure_daily(days=days)


class DailySyncScheduler:
    """Daily incremental-sync scheduler."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.last_run: Optional[datetime] = None
        self.last_result: Optional[dict] = None

    def _next_run_time(self, now: Optional[datetime] = None) -> datetime:
        """Compute the next run time (today 21:00 or tomorrow 21:00)."""
        now = now or datetime.now()
        target = now.replace(hour=SYNC_HOUR, minute=SYNC_MINUTE,
                             second=0, microsecond=0)
        if now >= target:
            # Already past today 21:00 -> tomorrow
            target += timedelta(days=1)
        return target

    def _seconds_until(self, target: datetime) -> float:
        return max(0.0, (target - datetime.now()).total_seconds())

    async def _run_sync(self, include_financial: bool = True,
                        start: Optional[date] = None,
                        end: Optional[date] = None) -> dict:
        """Run one incremental sync (quotes + optional financial announcements)."""
        logger.info("开始每日增量同步")
        from tools.market.sync_tushare import TusharePgSyncer

        end = end or date.today()
        # Look back N trading days (or use the caller-specified start)
        if start is None:
            start = end
            back = 0
            while back < BACKFILL_DAYS:
                start -= timedelta(days=1)
                if is_trading_day(start):
                    back += 1

        syncer = TusharePgSyncer()
        try:
            # One run covers multiple tables (internally reuses
            # connection/whitelist/calendar); blocking IO goes to the thread pool
            result = await asyncio.to_thread(
                syncer.run, start, end,
                ["daily", "daily_basic", "adj_factor"],
            )
        except Exception as e:
            logger.error(f"增量同步异常: {e}")
            result = {"error": str(e)}

        # Financial-report increment: pull reports announced within the last
        # FIN_BACKFILL_DAYS days (run in the thread pool, not blocking the event loop)
        if include_financial:
            try:
                fin_result = await asyncio.to_thread(
                    _run_financial_incremental, FIN_BACKFILL_DAYS)
                if isinstance(result, dict):
                    result["financial"] = fin_result
            except Exception as e:
                logger.error(f"财报增量同步异常: {e}")
                if isinstance(result, dict):
                    result["financial"] = {"error": str(e)}

            # Dividend/bonus-share increment: per-day batch over three channels
            # (ann_date/imp_ann_date/ex_date), 6 calls per day
            try:
                div_rows = await asyncio.to_thread(
                    _run_dividend_daily, 2)
                if isinstance(result, dict):
                    result["dividend"] = div_rows
            except Exception as e:
                logger.error(f"分红增量同步异常: {e}")
                if isinstance(result, dict):
                    result["dividend"] = {"error": str(e)}

            # Disclosure-date increment: per-day over two channels (ann_date =
            # planned announcement/changes, actual_date = actual disclosure)
            try:
                dis_rows = await asyncio.to_thread(
                    _run_disclosure_daily, 2)
                if isinstance(result, dict):
                    result["disclosure_date"] = dis_rows
            except Exception as e:
                logger.error(f"披露日期增量同步异常: {e}")
                if isinstance(result, dict):
                    result["disclosure_date"] = {"error": str(e)}

        self.last_run = datetime.now()
        self.last_result = result
        logger.info(f"每日增量同步完成: {result}")
        return result

    @staticmethod
    def _expected_latest_trade_date(now: Optional[datetime] = None) -> Optional[date]:
        """
        Compute the latest trading date whose data 'should already be ingested':
        - After 16:00 on a trading day -> today (Tushare ingests the day's data at 15-16)
        - Otherwise                     -> previous trading day
        """
        now = now or datetime.now()
        d = now.date()
        if is_trading_day(d) and now.hour >= 16:
            return d
        d -= timedelta(days=1)
        for _ in range(20):  # covers the longest holidays
            if is_trading_day(d):
                return d
            d -= timedelta(days=1)
        return None

    async def _catchup_if_missed(self):
        """
        Startup self-check catch-up: whenever started, compare the 'expected latest
        trading date' with the actual latest daily row in the DB; if there is a gap,
        backfill it immediately (covers: downtime during the scheduled slot /
        weekend restarts / post-holiday restarts / missed runs due to PG being
        unreachable — all scenarios). Quotes only (minutes-level work); financials
        are left to the routine 21:00 task.
        """
        expected = self._expected_latest_trade_date()
        if expected is None:
            logger.warning("无法计算应有最新交易日, 跳过启动自检")
            return

        try:
            from storage.pg import PgClient
            with PgClient() as pg:
                row = pg.fetch_one("SELECT MAX(trade_date) m FROM stock.daily")
                latest = row["m"] if row else None
        except Exception as e:
            logger.error(f"启动自检查询失败 (PG 不可达?): {e}")
            return

        if latest is not None and latest >= expected:
            logger.info(f"启动自检: 行情已最新 (daily 至 {latest} >= 应有 {expected}), 无需补漏")
            return

        # Gap start: the day after the DB's latest; with no data, look back 10
        # trading days
        if latest is not None:
            start = latest + timedelta(days=1)
        else:
            start = expected
            back = 0
            while back < 10:
                start -= timedelta(days=1)
                if is_trading_day(start):
                    back += 1

        logger.info(
            f"启动自检: 发现行情缺口 (库内最新 {latest}, 应有 {expected}), "
            f"立即补齐 {start} ~ {expected}"
        )
        try:
            await self._run_sync(include_financial=False,
                                 start=start, end=expected)
        except Exception as e:
            logger.error(f"启动补漏失败: {e}")

    async def _loop(self):
        """Scheduling loop."""
        logger.info(f"增量同步调度器已启动 (每日 {SYNC_HOUR}:{SYNC_MINUTE:02d})")

        # Startup self-check: compare expected latest trading date with the DB;
        # backfill any gap immediately (works at any start time, weekends, post-holiday)
        await self._catchup_if_missed()

        while not self._stop_event.is_set():
            next_run = self._next_run_time()
            # Run on trading days only
            if is_trading_day(next_run.date()):
                wait = self._seconds_until(next_run)
                logger.info(f"下次增量同步: {next_run} (等待 {wait:.0f}s)")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait
                    )
                    break  # stop signal received
                except asyncio.TimeoutError:
                    await self._run_sync()
            else:
                # Non-trading day -> skip to tomorrow
                wait = self._seconds_until(next_run)
                logger.debug(f"非交易日 {next_run.date()}, 等待 {wait:.0f}s")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait
                    )
                    break
                except asyncio.TimeoutError:
                    continue

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
        """Manually trigger one sync immediately."""
        return await self._run_sync()


# Singleton
_scheduler: Optional[DailySyncScheduler] = None


def get_scheduler() -> DailySyncScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DailySyncScheduler()
    return _scheduler
