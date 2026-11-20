"""Every-calendar-day Shanghai schedule for the attributed research briefing."""
import asyncio
from datetime import date, datetime, time, timedelta

from core.local_state import file_lock, read_json, write_json
from core.logger import get_logger
from tools.market.refresh_clock import SHANGHAI, market_time
from workflows.reports import send_morning_digest as morning
from workflows.runtime.scheduled_task import ScheduledTask

logger = get_logger(__name__)


def _first_issue(started):
    # After 07:30, a fresh installation starts tomorrow. A later restart can
    # recover today's missed slot because the original first date is durable.
    root = morning.output_root()
    with file_lock(root / "schedule.lock") as acquired:
        if not acquired:
            return None
        path = root / "schedule.json"
        state = read_json(path, strict=True)
        if state:
            return date.fromisoformat(state["first_issue"])
        first = started.date() + timedelta(days=int(started.time() > morning.SEND_TIME))
        write_json(path, {"first_issue": first.isoformat(), "enabled_at": started.isoformat()})
        return first


class ReportDigestScheduler(ScheduledTask):
    def __init__(self):
        super().__init__("研报观点早报")
        self.last_run = None
        self.sending = False

    @property
    def enabled(self):
        return morning.enabled()

    def status(self):
        return {
            "enabled": self.enabled, "running": self.running, "sending": self.sending,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_result": self.last_result, "time_zone": "Asia/Shanghai",
            "schedule_time": morning.SEND_TIME.isoformat(timespec="minutes"), "owner": "backend",
        }

    async def _loop(self):
        started = market_time()
        first = handled = None
        while not self._stop.is_set():
            if first is None:
                try:
                    first = await asyncio.to_thread(_first_issue, started)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    self.last_result = {"status": "blocked", "error_type": type(exc).__name__}
                    logger.warning("研报早报启用记录不可用: %s", type(exc).__name__)
                if first is None:
                    self.next_run = market_time() + timedelta(minutes=1)
                    await self._wait(60)
                    continue
            now = market_time()
            day = now.date()
            slot = datetime.combine(max(day, first), morning.SEND_TIME, SHANGHAI)
            if now >= slot and day != handled:
                self.sending, self.next_run, self.last_run = True, None, now
                try:
                    self.last_result = await asyncio.to_thread(morning.attempt, day)
                finally:
                    self.sending = False
                status = self.last_result["status"]
                logger.info("研报观点早报 %s: %s", day, status)
                if status in ("busy", "retry"):
                    midnight = datetime.combine(day + timedelta(days=1), time.min, SHANGHAI)
                    self.next_run = min(midnight, market_time() + timedelta(seconds=morning.RETRY_SECONDS))
                    await self._wait((self.next_run - market_time()).total_seconds())
                else:
                    handled = day
                continue
            self.next_run = slot if slot > now else slot + timedelta(days=1)
            logger.info("下次研报观点早报: %s", self.next_run.isoformat())
            await self._wait((self.next_run - market_time()).total_seconds())


_scheduler = None


def get_report_digest_scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = ReportDigestScheduler()
    return _scheduler
