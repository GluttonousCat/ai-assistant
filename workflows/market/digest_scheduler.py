"""Backend-owned midday and closing email schedules in Shanghai time."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta

from core.config import get_config
from core.logger import get_logger
from workflows.market.send_close_digest import deliver_once
from workflows.market import send_midday_digest as midday
from workflows.runtime.scheduled_task import ScheduledTask
from tools.kline.calendar import is_trading_day
from tools.market.refresh_clock import SHANGHAI, market_now, market_time

logger = get_logger(__name__)
SEND_TIME = time(18)
RETRY_UNTIL = time(20)
RETRY_SECONDS = 600


class MarketDigestScheduler(ScheduledTask):
    send_time = SEND_TIME
    retry_until = RETRY_UNTIL
    retry_seconds = RETRY_SECONDS
    send_delay_notice = True
    label = "盘后趋势邮件"

    def __init__(self):
        super().__init__(self.label)
        self._lock = asyncio.Lock()
        self.last_run = None

    @property
    def enabled(self):
        cfg = get_config()
        value = cfg.env("MARKET_DIGEST_ENABLED", cfg.get("email_digest.enabled", True))
        return str(value).lower() in ("true", "1", "yes")

    @classmethod
    def next_slot(cls, now=None):
        now = market_time(now or market_now())
        slot = datetime.combine(now.date(), cls.send_time, SHANGHAI)
        if now >= slot:
            slot += timedelta(days=1)
        while not is_trading_day(slot.date()):
            slot += timedelta(days=1)
        return slot

    def initial_due(self, now):
        return is_trading_day(now.date()) and now.time() >= self.send_time

    def deliver(self, *, delay_notice=False):
        return deliver_once(delay_notice=delay_notice)

    def status(self):
        return {
            "enabled": self.enabled,
            "running": self.running,
            "sending": self._lock.locked(),
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_result": self.last_result,
            "time_zone": "Asia/Shanghai",
            "schedule_time": self.send_time.isoformat(timespec="minutes"),
            "owner": "backend",
        }

    async def _attempt(self, *, delay_notice=False):
        self.last_run = market_now()
        self.next_run = None
        try:
            result = await asyncio.to_thread(self.deliver, delay_notice=delay_notice)
        except Exception as error:
            # SMTP errors and diagnostics may contain account/server details.
            # Expose only the type, retaining the existing persistent receipts.
            result = {"status": "retry", "error_type": type(error).__name__}
            logger.warning("%s异常: %s", self.label, type(error).__name__)
        self.last_result = result
        logger.info("%s结果: status=%s, count=%s, delay=%s",
                    self.label, result.get("status"), result.get("count"), delay_notice)
        return result

    async def _deliver_window(self):
        async with self._lock:
            day = market_now().date()
            deadline = datetime.combine(day, self.retry_until, SHANGHAI)
            while not self._stop.is_set() and market_now().date() == day:
                result = await self._attempt()
                if result.get("status") not in ("waiting_data", "retry", "busy"):
                    return result
                now = market_now()
                if now.date() != day:
                    return result  # Do not roll an old retry into tomorrow.
                if now >= deadline:
                    if self.send_delay_notice and result.get("status") == "waiting_data" and not self._stop.is_set():
                        result = await self._attempt(delay_notice=True)
                        if result.get("status") == "retry":
                            # The warehouse may have completed between checks.
                            result = await self._attempt()
                    return result
                wait = min(self.retry_seconds, (deadline-now).total_seconds())
                self.next_run = now + timedelta(seconds=wait)
                if self.send_delay_notice or result["status"] != "waiting_data":
                    logger.info("%s待重试: %s", self.label, self.next_run.isoformat())
                if await self._wait(wait):
                    return result

    async def _loop(self):
        logger.info("%s调度已启动: %s Asia/Shanghai", self.label, self.send_time.isoformat(timespec="minutes"))
        # Only today's run is recovered; durable per-day receipts make this safe
        # for a restart after 18:00, including a restart after the retry deadline.
        if self.initial_due(market_now()):
            await self._deliver_window()
        while not self._stop.is_set():
            self.next_run = self.next_slot()
            logger.info("下次%s: %s", self.label, self.next_run.isoformat())
            if await self._wait((self.next_run-market_now()).total_seconds()):
                break
            await self._deliver_window()


class MiddayDigestScheduler(MarketDigestScheduler):
    send_time = midday.SEND_TIME
    retry_until = midday.RETRY_UNTIL
    retry_seconds = 60
    label = "午间60F邮件"

    @property
    def enabled(self):
        cfg = get_config()
        value = cfg.env("MIDDAY_DIGEST_ENABLED", cfg.get("email_digest.midday_enabled", True))
        return super().enabled and str(value).lower() in ("true", "1", "yes")

    def initial_due(self, now):
        return is_trading_day(now.date()) and self.send_time <= now.time() <= self.retry_until

    def deliver(self, *, delay_notice=False):
        now = market_now()
        return midday.deliver_midday_once(now, deadline_notice=delay_notice or now.time() >= self.retry_until)


_scheduler = None
_midday_scheduler = None


def get_digest_scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = MarketDigestScheduler()
    return _scheduler


def get_midday_scheduler():
    global _midday_scheduler
    if _midday_scheduler is None:
        _midday_scheduler = MiddayDigestScheduler()
    return _midday_scheduler
