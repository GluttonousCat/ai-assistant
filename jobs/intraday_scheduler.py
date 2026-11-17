"""Four closed-60F captures, serialized and restart-safe, owned by the backend."""
import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.config import get_config
from core.local_state import file_lock, read_json, write_json
from core.logger import get_logger
from jobs.cache_intraday_prefix import SLOTS, collect
from jobs.scan_intraday_watch import run as scan
from storage.repositories.intraday_watch import root, read_report, publish_report
from tools.kline.calendar import is_trading_day

logger = get_logger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
SETTLE_SECONDS = 120
RETRY_SECONDS = 300
MAX_ATTEMPTS = 3


def market_now():
    return datetime.now(SHANGHAI)


def local(now):
    return now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)


def slots_for(day):
    if not is_trading_day(day):
        return []
    return [(slot, datetime.combine(day, time.fromisoformat(slot), SHANGHAI) + timedelta(seconds=SETTLE_SECONDS)) for slot in SLOTS]


def due_slot(now):
    """Catch up only the newest slot today; never replay older missed sessions."""
    now = local(now)
    due = [slot for slot, at in slots_for(now.date()) if at <= now]
    return due[-1] if due else None


def next_slot(now):
    now = local(now)
    for offset in range(32):
        for _, at in slots_for(now.date() + timedelta(days=offset)):
            if at > now:
                return at
    raise ValueError("未来32天没有可用交易时点")


class IntradayScheduler:
    def __init__(self):
        self._task = None
        self._stop = asyncio.Event()
        self.next_run = None
        self.last_result = None

    @property
    def enabled(self):
        cfg = get_config()
        return str(cfg.env("INTRADAY_WATCH_ENABLED", cfg.get("intraday_watch.enabled", True))).lower() in ("true", "1", "yes")

    def status(self):
        return {"enabled": self.enabled, "running": self._task is not None and not self._task.done(),
                "next_run": self.next_run.isoformat() if self.next_run else None,
                "last_result": self.last_result, "time_zone": "Asia/Shanghai", "owner": "backend",
                "close_times": list(SLOTS), "settle_seconds": SETTLE_SECONDS}

    def tick(self, now=None):
        """Blocking unit, run off the event loop; the OS lock covers the pipeline."""
        now = local(now or market_now())
        slot = due_slot(now)
        if slot is None:
            return {"status": "waiting"}
        run_id = f"{now.date()}_{slot.replace(':', '')}"
        folder = root() / run_id
        with file_lock(root() / "scheduler.lock") as acquired:
            if not acquired:
                return {"status": "busy", "run_id": run_id}
            receipt = read_json(folder / "scheduled.json", strict=True)
            # A manually published run also satisfies this slot. No re-fetch on restart.
            report = read_report(run_id)
            if report and report.get("as_of") == f"{now.date()} {slot}:00":
                latest = read_report()
                if not latest or latest["as_of"] < report["as_of"]:
                    publish_report(report)
                if receipt and receipt.get("status") != "complete":
                    receipt.update(status="complete", updated_at=now.isoformat())
                    write_json(folder / "scheduled.json", receipt)
                    write_json(root() / "scheduler.json", receipt)
                return {"status": "complete", "run_id": run_id}
            if receipt.get("attempts", 0) >= MAX_ATTEMPTS:
                return {"status": "exhausted", "run_id": run_id}
            if receipt.get("retry_at") and now < datetime.fromisoformat(receipt["retry_at"]):
                return {"status": "retry_wait", "run_id": run_id, "retry_at": receipt["retry_at"]}
            receipt = {"run_id": run_id, "attempts": receipt.get("attempts", 0) + 1,
                       "started_at": now.isoformat(), "status": "collecting"}

            def save(stage):
                receipt.update(status=stage, updated_at=market_now().isoformat())
                write_json(folder / "scheduled.json", receipt)
                write_json(root() / "scheduler.json", receipt)

            try:
                save("collecting")
                workers = max(1, min(4, int(get_config().get("intraday_watch.workers", 4))))
                logger.info("后台60F更新开始: %s，尝试%s", run_id, receipt["attempts"])
                collect(str(now.date()), slot, workers=workers)
                if self._stop.is_set():
                    raise InterruptedError("后台停止，保留采集断点")
                save("scanning")
                result = scan(str(now.date()), slot)
                receipt["result"] = result
                save("complete")
                logger.info("后台60F更新完成: %s，候选%s", run_id, result["candidates"])
            except Exception as error:
                # Keep private provider/DB diagnostics out of the public status.
                receipt.update(error_type=type(error).__name__, retry_at=(market_now() + timedelta(seconds=RETRY_SECONDS)).isoformat())
                save("error")
                logger.warning("后台60F更新失败: %s %s", run_id, type(error).__name__)
            return receipt

    async def _loop(self):
        logger.info("后台60F调度启动: 交易日10:32/11:32/14:02/15:02 Asia/Shanghai")
        while not self._stop.is_set():
            try:
                self.last_result = await asyncio.to_thread(self.tick)
                self.next_run = next_slot(market_now())
                retry = self.last_result.get("retry_at")
                if retry and self.last_result.get("attempts", 0) < MAX_ATTEMPTS:
                    self.next_run = min(self.next_run, datetime.fromisoformat(retry))
            except Exception as error:
                logger.warning("后台60F调度检查失败: %s", type(error).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    def start(self):
        if self.enabled and (self._task is None or self._task.done()):
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="intraday-60f-scheduler")

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


_scheduler = None


def get_intraday_scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = IntradayScheduler()
    return _scheduler
