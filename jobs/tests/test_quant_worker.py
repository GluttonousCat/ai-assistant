from uuid import uuid4
import asyncio
from types import SimpleNamespace
import pytest

from jobs import quant_worker
from skills.quant.workspace_models import StrategyDraft
from storage.repositories import quant_workspace as store


def test_worker_completion_failure_and_os_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_root", lambda: tmp_path)
    draft = store.save_draft(1, StrategyDraft().model_dump())

    def enqueue():
        return store.enqueue(
            1,
            dict(
                draft_id=draft["id"],
                request_id=str(uuid4()),
                start="2026-01-01",
                end="2026-11-12",
                sample_limit=50,
            ),
        )

    task = enqueue()
    result = {"metrics": {"closed_trades": 0}, "trades": []}
    monkeypatch.setattr(quant_worker, "run_task", lambda *args: result)
    with store.worker_lock() as acquired:
        assert acquired
        quant_worker.process_queue()
        assert store.task(1, task["id"])["status"] == "queued"
    quant_worker.process_queue()
    assert store.task(1, task["id"])["status"] == "completed"
    assert store.load_result(1, task["id"])["metrics"] == result["metrics"]
    task = enqueue()

    def fail(*args):
        raise RuntimeError("private database host or password")

    monkeypatch.setattr(quant_worker, "run_task", fail)
    quant_worker.process_queue()
    row = store.task(1, task["id"])
    assert row["status"] == "failed" and "private" not in row["progress"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("completed", "completed"),
        ("crash", "failed"),
        ("shutdown", "interrupted"),
        ("timeout", "failed"),
    ],
)
def test_supervisor_bounds_child_lifetime_and_recovers_queue(
    monkeypatch, tmp_path, mode, expected
):
    monkeypatch.setattr(store, "_root", lambda: tmp_path)
    draft = store.save_draft(1, StrategyDraft().model_dump())
    task = store.enqueue(
        1,
        dict(
            draft_id=draft["id"],
            request_id=str(uuid4()),
            start="2026-01-01",
            end="2026-11-12",
            sample_limit=50,
        ),
    )
    monkeypatch.setattr(
        quant_worker,
        "get_config",
        lambda: SimpleNamespace(root_dir=str(tmp_path), get=lambda *args: 10),
    )
    killed = []

    async def scenario():
        stop = asyncio.Event()
        ended = asyncio.Event()
        process = SimpleNamespace(returncode=None, pid=123)

        async def wait():
            await ended.wait()
            return process.returncode

        process.wait = wait

        async def create(*args, **kwargs):
            assert args[1:3] == ("-m", "jobs.quant_worker")
            if mode == "completed":
                store.update_task(task["id"], status="completed")
                process.returncode = 0
                ended.set()
            elif mode == "crash":
                process.returncode = 1
                ended.set()
            elif mode == "shutdown":
                stop.set()
            return process

        async def terminate(child):
            killed.append(child.pid)
            child.returncode = -1
            ended.set()

        monkeypatch.setattr(quant_worker.asyncio, "create_subprocess_exec", create)
        monkeypatch.setattr(quant_worker, "_terminate", terminate)
        if mode == "timeout":

            async def timed_out(*args, **kwargs):
                return set(), set(args[0])

            monkeypatch.setattr(quant_worker.asyncio, "wait", timed_out)
        await quant_worker.consume(stop)

    asyncio.run(scenario())
    assert store.task(1, task["id"])["status"] == expected
    assert killed == ([123] if mode in ("shutdown", "timeout") else [])
    with store.worker_lock() as acquired:
        assert acquired
