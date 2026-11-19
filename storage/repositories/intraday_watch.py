"""Atomic, isolated intraday report files; never replace official scan tables."""
from functools import lru_cache
from pathlib import Path
import re

from core.config import get_config
from core.local_state import read_json, write_json, file_lock


def root():
    return Path(get_config().root_dir) / "output" / "market" / "intraday_watch"


def _report_path(run_id=None):
    if run_id and not re.fullmatch(r"\d{4}-\d{2}-\d{2}_(1030|1130|1400|1500)", run_id):
        raise ValueError("盘中报告标识不合法")
    return root() / run_id / "report.json" if run_id else root() / "latest.json"


def read_report(run_id=None):
    return read_json(_report_path(run_id), strict=True) or None


@lru_cache(maxsize=16)
def _read_cutoff(path, file_version):
    # Writers atomically replace reports. Cache only a cutoff for this file version.
    return read_json(path, strict=True).get("as_of")


def _report_cutoff(run_id=None):
    path = _report_path(run_id)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return _read_cutoff(path, (stat.st_ino, stat.st_mtime_ns, stat.st_size))


def reconcile_publication(run_id, expected_as_of):
    """Confirm an archived slot and restore latest after an interrupted publication."""
    if _report_cutoff(run_id) != expected_as_of:
        return False
    latest = _report_cutoff()
    if latest and latest >= expected_as_of:
        return True
    report = read_report(run_id)
    if not report or report.get("as_of") != expected_as_of:
        return False
    publish_report(report)
    return True


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
