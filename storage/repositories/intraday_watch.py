"""Atomic, isolated intraday report files; never replace official scan tables."""
from pathlib import Path
import re

from core.config import get_config
from core.local_state import read_json, write_json, file_lock


def root():
    return Path(get_config().root_dir) / "output" / "market" / "intraday_watch"


def read_report(run_id=None):
    if run_id and not re.fullmatch(r"\d{4}-\d{2}-\d{2}_(1030|1130|1400|1500)", run_id):
        raise ValueError("盘中报告标识不合法")
    path = root() / run_id / "report.json" if run_id else root() / "latest.json"
    return read_json(path, strict=True) or None


def publish_report(report):
    with file_lock(root() / "publication.lock") as acquired:
        if not acquired:
            raise RuntimeError("另一个盘中结果正在发布，请稍后重试")
        previous = read_report()
        if not previous or report["as_of"] >= previous["as_of"]:
            write_json(root() / "latest.json", report)


def scheduler_state():
    state = read_json(root() / "scheduler.json", strict=True)
    if state.get("status") in ("collecting", "scanning"):
        filename = "progress.json" if state["status"] == "collecting" else "analysis_progress.json"
        state["progress"] = read_json(root() / state["run_id"] / filename, strict=True)
    return state
