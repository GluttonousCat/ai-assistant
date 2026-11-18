import asyncio
from types import SimpleNamespace

import pytest

from jobs import lifecycle, quant_worker, zsxq_scheduler
from tools.auth import users


def test_shutdown_cleans_started_jobs_on_application_error_and_continues_after_stop_failure(
    monkeypatch,
):
    calls = []

    async def worker(stop):
        await stop.wait()
        calls.append("worker-stop")

    def scheduler(name, *, failed_start=False, failed_stop=False):
        def start():
            calls.append(name + "-start")
            if failed_start:
                raise RuntimeError("start")

        async def stop():
            calls.append(name + "-stop")
            if failed_stop:
                raise RuntimeError("stop")

        return SimpleNamespace(start=start, stop=stop)

    monkeypatch.setattr(
        lifecycle,
        "get_config",
        lambda: SimpleNamespace(get=lambda key, default: default),
    )
    monkeypatch.setattr(users, "ensure_auth_schema", lambda: None)
    monkeypatch.setattr(quant_worker, "run_worker", worker)
    monkeypatch.setattr(lifecycle, "get_scheduler", lambda: scheduler("daily"))
    monkeypatch.setattr(
        lifecycle,
        "get_intraday_scheduler",
        lambda: scheduler("intraday", failed_start=True),
    )
    monkeypatch.setattr(
        lifecycle, "get_digest_scheduler", lambda: scheduler("mail", failed_stop=True)
    )
    monkeypatch.setattr(
        zsxq_scheduler, "get_zsxq_scheduler", lambda: scheduler("reports")
    )

    async def scenario():
        async with lifecycle.lifespan(SimpleNamespace(state=SimpleNamespace())):
            raise ValueError("application failure")

    with pytest.raises(ValueError, match="application failure"):
        asyncio.run(scenario())
    assert calls == [
        "daily-start",
        "intraday-start",
        "mail-start",
        "reports-start",
        "worker-stop",
        "reports-stop",
        "mail-stop",
        "daily-stop",
    ]
