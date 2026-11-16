"""Backend-owned daily 18:00 Shanghai email scheduling, independent of AI."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.config import get_config
from core.logger import get_logger
from jobs.send_market_digest import deliver_once

logger = get_logger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
SEND_TIME = time(18)
RETRY_UNTIL = time(20)
RETRY_SECONDS = 600


def market_now():
    return datetime.now(SHANGHAI)


def _local(now):
    return now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)


class MarketDigestScheduler:
    def __init__(self):
        self._task = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self.next_run = None
        self.last_run = None
        self.last_result = None

    @property
    def enabled(self):
        cfg = get_config()
        value = cfg.env("MARKET_DIGEST_ENABLED", cfg.get("email_digest.enabled", True))
        return str(value).lower() in ("true", "1", "yes")

    @staticmethod
    def next_slot(now=None):
        now = _local(now or market_now())
        slot = datetime.combine(now.date(), SEND_TIME, SHANGHAI)
        return slot if now < slot else slot + timedelta(days=1)

    def status(self):
        return {
            "enabled": self.enabled,
            "running": self._task is not None and not self._task.done(),
            "sending": self._lock.locked(),
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_result": self.last_result,
            "time_zone": "Asia/Shanghai",
            "schedule_time": "18:00",
            "owner": "backend",
        }

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    async def _attempt(self, *, delay_notice=False):
        self.last_run = market_now()
        self.next_run = None
        try:
            result = await asyncio.to_thread(deliver_once, delay_notice=delay_notice)
        except Exception as error:
            # SMTP errors and diagnostics may contain account/server details.
            # Expose only the type, retaining the existing persistent receipts.
            result = {"status": "retry", "error_type": type(error).__name__}
            logger.warning("后台趋势邮件异常: %s", type(error).__name__)
        self.last_result = result
        logger.info("后台趋势邮件结果: status=%s, count=%s, delay=%s",
                    result.get("status"), result.get("count"), delay_notice)
        return result

    async def _deliver_window(self):
        async with self._lock:
            day = market_now().date()
            deadline = datetime.combine(day, RETRY_UNTIL, SHANGHAI)
            while not self._stop.is_set() and market_now().date() == day:
                result = await self._attempt()
                if result.get("status") not in ("waiting_data", "retry", "busy"):
                    return result
                now = market_now()
                if now.date() != day:
                    return result  # Do not roll an old retry into tomorrow.
                if now >= deadline:
                    if result.get("status") == "waiting_data" and not self._stop.is_set():
                        result = await self._attempt(delay_notice=True)
                        if result.get("status") == "retry":
                            # The warehouse may have completed between checks.
                            result = await self._attempt()
                    return result
                wait = min(RETRY_SECONDS, (deadline-now).total_seconds())
                self.next_run = now + timedelta(seconds=wait)
                logger.info("后台趋势邮件待重试: %s", self.next_run.isoformat())
                if await self._wait(wait):
                    return result

    async def _loop(self):
        logger.info("后台趋势邮件调度已启动: 每日18:00 Asia/Shanghai")
        # Only today's run is recovered; durable per-day receipts make this safe
        # for a restart after 18:00, including a restart after the retry deadline.
        if market_now().time().replace(tzinfo=None) >= SEND_TIME:
            await self._deliver_window()
        while not self._stop.is_set():
            self.next_run = self.next_slot()
            logger.info("下次后台趋势邮件: %s", self.next_run.isoformat())
            if await self._wait((self.next_run-market_now()).total_seconds()):
                break
            await self._deliver_window()

    def start(self):
        if self.enabled and (self._task is None or self._task.done()):
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="market-digest-scheduler")

    async def stop(self):
        self._stop.set()
        self.next_run = None
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        logger.info("后台趋势邮件调度已停止")


_scheduler = None


def get_digest_scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = MarketDigestScheduler()
    return _scheduler
