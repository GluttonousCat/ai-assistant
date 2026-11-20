"""Independent schedules for report collection and off-peak document processing."""

import asyncio
from datetime import timedelta
from functools import lru_cache

from core.config import get_config
from core.local_state import file_lock, read_json, write_json
from core.logger import get_logger
from core.observability import observed, task_progress
from core.offpeak import is_offpeak, local_time
from core.offpeak_queue import enqueue, queue_root
from workflows.reports.offpeak_jobs import process_pending
from workflows.runtime.scheduled_task import ScheduledTask

logger = get_logger(__name__)


def latest_slot(now, hours):
    """Identify the latest scheduled round even after a process restart."""
    now = local_time(now)
    slots = [now.replace(hour=hour, minute=0, second=0, microsecond=0) for hour in hours]
    return max(slot if slot <= now else slot - timedelta(days=1) for slot in slots)


class ZSXQScheduleScheduler(ScheduledTask):
    def __init__(self):
        super().__init__("知识星球研报采集")
        self.run_hours = sorted({int(h) % 24 for h in
                                 (get_config().get("zsxq_schedule.run_hours") or [0, 12])})
        self.last_run = None

    @property
    def enabled(self):
        return get_config().get("zsxq_schedule.enabled", True)

    @observed("reports_round", "研报采集与入库")
    def _run_round(self, hour):
        from workflows.reports.fetch import run_once

        group = get_config().zsxq_group_id
        if not group:
            return {"error": "未配置 ZSXQ_GROUP_ID"}
        task_progress("抓取研报、下载并登记待分析附件")
        fetch = run_once(str(group), max_fetch=15, max_pages=10, crawl_topics=True)
        self.last_run = local_time()
        self.last_result = {"hour": hour, "fetch": fetch}
        topics, files = fetch.get("topics", {}), fetch.get("collect", {})
        if (fetch.get("error") or str(fetch.get("pg_sync", "")).startswith("error")
                or topics.get("error") or topics.get("errors") or files.get("error")):
            self.last_result["error"] = "研报抓取或入库未完成"
        return self.last_result

    def _run_due(self, slot, *, force=False):
        root = queue_root()
        with file_lock(root / "report-round.lock") as acquired:
            if not acquired:
                return {"status": "busy"}
            path = root / "report-round.json"
            state = read_json(path, strict=True)
            if not force and state.get("slot") == slot.isoformat():
                if not state.get("retry_at") or local_time().isoformat() < state["retry_at"]:
                    return {"status": "waiting"}
            try:
                result = self._run_round(slot.hour)
            except Exception as exc:
                logger.exception("研报采集轮次异常")
                result = {"error": type(exc).__name__}
            state = {"slot": slot.isoformat()}
            if result.get("error"):
                state["retry_at"] = (local_time() + timedelta(minutes=30)).isoformat()
            write_json(path, state)
            return result

    async def _loop(self):
        logger.info("研报采集北京时间 %s 点，启动时检查最近一轮", self.run_hours)
        while not self._stop.is_set():
            now = local_time()
            try:
                await asyncio.to_thread(self._run_due, latest_slot(now, self.run_hours))
            except Exception:
                logger.exception("研报采集调度异常")
            await self._wait(60)

    async def trigger_now(self, hour=None):
        now = local_time()
        slot = latest_slot(now, self.run_hours if hour is None else [hour])
        return await asyncio.to_thread(self._run_due, slot, force=True)


class OffPeakScheduler(ScheduledTask):
    def __init__(self):
        super().__init__("闲时模型解析")

    @property
    def enabled(self):
        return get_config().get("offpeak_processing.enabled", True)

    def _tick(self, now):
        if not is_offpeak(now):
            return
        root = queue_root()
        if get_config().get("zsxq_schedule.enabled", True):
            slot = latest_slot(now, (0, 22))
            with file_lock(root / "backlog-round.lock") as acquired:
                path = root / "backlog-round.json"
                if acquired and read_json(path, strict=True).get("slot") != slot.isoformat():
                    enqueue("report_metadata", limit=40)
                    enqueue("report_batch", limit=15 if slot.hour == 0 else 8)
                    if slot.hour == 0:
                        enqueue("report_digest")
                    write_json(path, {"slot": slot.isoformat()})
        if any((root / "pending").glob("*.json")):
            process_pending()

    async def _loop(self):
        logger.info("后台模型解析每日北京时间 22:00–08:00 运行")
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._tick, local_time())
            except Exception:
                logger.exception("闲时文档调度异常")
            await self._wait(60)


@lru_cache(maxsize=1)
def get_zsxq_scheduler():
    return ZSXQScheduleScheduler()


@lru_cache(maxsize=1)
def get_offpeak_scheduler():
    return OffPeakScheduler()
