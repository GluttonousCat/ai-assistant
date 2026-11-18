from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from core import observability as obs
from core.local_state import read_json
from storage.repositories import observability as store


@pytest.fixture
def root(tmp_path, monkeypatch):
    config = lambda: SimpleNamespace(root_dir=tmp_path)
    monkeypatch.setattr(obs, "get_config", config)
    monkeypatch.setattr(store, "get_config", config)
    return tmp_path


def test_live_lease_nested_parent_and_final_progress(root):
    @obs.observed("child", "下载研报")
    def child():
        obs.task_progress("下载完成，反检测等待", completed=1, total=3)
        rows = store.task_snapshot()["active"]
        assert len(rows) == 2
        c = next(r for r in rows if r["kind"] == "child")
        assert c["parent_id"] == next(r for r in rows if r["kind"] == "parent")["id"]
        assert (c["completed"], c["total"]) == (1, 3)

    @obs.observed("parent", "研报流水线")
    def parent():
        child()
        obs.task_progress("收尾入库")

    parent()
    state = store.task_snapshot()
    assert not state["active"]
    assert len(state["history"]) == 2
    assert all(r["status"] == "completed" for r in state["history"])


def test_failure_and_warning_are_persisted_without_swallowing(root, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "secret-value-123")

    @obs.observed("broken", "失败任务")
    def broken():
        raise ValueError("secret-value-123")

    with pytest.raises(ValueError):
        broken()
    row = store.task_snapshot()["history"][0]
    assert row["status"] == "failed" and row["ended_at"]
    assert "secret-value-123" not in str(row)

    @obs.observed("partial", "部分完成")
    def partial():
        obs.task_progress(warning="一只股票缺数据")

    partial()
    assert store.task_snapshot()["history"][0]["status"] == "partial"


def test_concurrent_runs_are_isolated_and_events_bounded(root):
    @obs.observed("many", "并行任务")
    def work(index):
        for n in range(45):
            obs.task_progress(f"任务{index}阶段{n}")
        return index

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(work, range(4))) == list(range(4))
    rows = store.task_snapshot()["history"]
    assert len({r["id"] for r in rows}) == 4
    assert all(len(r["events"]) == 40 for r in rows)


def test_orphaned_run_does_not_remain_running(root):
    task = obs.TaskObservation("orphan", "崩溃任务")
    task.update("下载中")
    assert read_json(obs.runs_root() / f"{task.key}.json")["status"] == "running"
    state = store.task_snapshot()
    assert not state["active"]
    assert state["history"][0]["status"] == "interrupted"


def test_observer_io_failure_preserves_business_result(root, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(obs, "write_json", fail)

    @obs.observed("safe", "主业务")
    def business():
        obs.task_progress("处理中")
        return 42

    assert business() == 42


def test_silent_live_run_remains_visible_past_recent_history_window(root):
    from core.local_state import write_json

    @obs.observed("long", "长任务")
    def long():
        for n in range(205):
            write_json(
                obs.runs_root() / f"finished-{n}.json",
                {"id": str(n), "status": "completed", "updated_at": "2026-11-18"},
            )
        assert any(r["kind"] == "long" for r in store.task_snapshot()["active"])

    long()


def test_real_process_exit_releases_lease_and_is_reported_interrupted(root):
    import subprocess
    import sys

    script = """
import os, sys
from pathlib import Path
from core import observability as obs
from core.local_state import file_lock
obs.runs_root = lambda: Path(sys.argv[1])
task = obs.TaskObservation("crash", "crash")
with file_lock(obs.runs_root() / (task.key + ".lock")):
    task.update("downloading")
    os._exit(3)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(obs.runs_root())], timeout=10
    )
    assert result.returncode == 3
    assert store.task_snapshot()["history"][0]["status"] == "interrupted"
