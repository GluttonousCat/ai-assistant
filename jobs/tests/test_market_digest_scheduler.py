import asyncio
from datetime import datetime, timezone
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jobs import market_digest_scheduler as module

SHANGHAI = ZoneInfo("Asia/Shanghai")


def at(hour, minute=0, day=16):
    return datetime(2026, 11, day, hour, minute, tzinfo=SHANGHAI)


def test_clock_is_shanghai_and_weekends_also_have_a_slot():
    assert module.MarketDigestScheduler.next_slot(
        datetime(2026, 11, 16, 9, 59, tzinfo=timezone.utc)
    ) == at(18)
    assert module.MarketDigestScheduler.next_slot(
        datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
    ) == at(18, day=17)
    assert module.MarketDigestScheduler.next_slot(at(17, day=14)) == at(18, day=14)


def test_startup_before_18_waits_without_sending(monkeypatch):
    monkeypatch.setattr(module, "market_now", lambda: at(16))
    job = module.MarketDigestScheduler()

    async def never_send():
        pytest.fail("must wait until 18:00")

    async def wait(seconds):
        assert seconds == 7200
        assert job.next_run == at(18)
        job._stop.set()
        return True

    job._deliver_window, job._wait = never_send, wait
    asyncio.run(job._loop())


def test_startup_after_18_recovers_today_and_then_waits_until_tomorrow(monkeypatch):
    monkeypatch.setattr(module, "market_now", lambda: at(18, 35))
    calls = []
    job = module.MarketDigestScheduler()

    async def deliver():
        calls.append(True)

    async def wait(seconds):
        assert job.next_run == at(18, day=17)
        job._stop.set()
        return True

    job._deliver_window, job._wait = deliver, wait
    asyncio.run(job._loop())
    assert calls == [True]


def test_delivery_io_is_off_loop_and_data_delay_retries(monkeypatch):
    main_thread = threading.get_ident()
    threads, notices, waits = [], [], []
    monkeypatch.setattr(module, "market_now", lambda: at(18))

    def send(*, delay_notice=False):
        threads.append(threading.get_ident())
        notices.append(delay_notice)
        return {"status": "waiting_data" if len(threads) == 1 else "sent"}

    monkeypatch.setattr(module, "deliver_once", send)
    job = module.MarketDigestScheduler()

    async def wait(seconds):
        waits.append(seconds)
        return False

    job._wait = wait
    result = asyncio.run(job._deliver_window())
    assert result["status"] == "sent" and waits == [600]
    assert notices == [False, False] and all(t != main_thread for t in threads)


@pytest.mark.parametrize("status", ["sent", "blocked", "uncertain"])
def test_terminal_status_does_not_retry_or_ignore_receipts(monkeypatch, status):
    monkeypatch.setattr(module, "market_now", lambda: at(18))
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        return {"status": status, "already_processed": status == "sent"}

    monkeypatch.setattr(module, "deliver_once", send)
    job = module.MarketDigestScheduler()

    async def no_wait(seconds):
        pytest.fail("terminal result must not retry")

    job._wait = no_wait
    result = asyncio.run(job._deliver_window())
    assert result["status"] == status and len(calls) == 1


def test_missing_data_after_deadline_only_sends_delay_notice(monkeypatch):
    monkeypatch.setattr(module, "market_now", lambda: at(20, 10))
    notices = []

    def send(*, delay_notice=False):
        notices.append(delay_notice)
        return {"status": "sent" if delay_notice else "waiting_data"}

    monkeypatch.setattr(module, "deliver_once", send)
    result = asyncio.run(module.MarketDigestScheduler()._deliver_window())
    assert result["status"] == "sent" and notices == [False, True]


def test_stop_during_wait_prevents_another_send(monkeypatch):
    monkeypatch.setattr(module, "market_now", lambda: at(18))
    calls = []
    monkeypatch.setattr(
        module, "deliver_once", lambda **kw: calls.append(kw) or {"status": "retry"}
    )
    job = module.MarketDigestScheduler()

    async def stop(seconds):
        job._stop.set()
        return True

    job._wait = stop
    asyncio.run(job._deliver_window())
    assert len(calls) == 1


def test_retry_does_not_turn_into_tomorrows_email(monkeypatch):
    state = {"completed": False}
    monkeypatch.setattr(
        module, "market_now", lambda: at(0, 1, 17) if state["completed"] else at(19, 59)
    )
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        state["completed"] = True
        return {"status": "retry"}

    monkeypatch.setattr(module, "deliver_once", send)
    result = asyncio.run(module.MarketDigestScheduler()._deliver_window())
    assert result["status"] == "retry" and len(calls) == 1


def test_unexpected_worker_error_keeps_retry_alive_without_exposing_text(monkeypatch):
    monkeypatch.setattr(module, "market_now", lambda: at(18))
    calls, seen = [], []

    def send(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("private SMTP response")
        return {"status": "sent"}

    monkeypatch.setattr(module, "deliver_once", send)
    job = module.MarketDigestScheduler()

    async def wait(seconds):
        seen.append(job.last_result.copy())
        return False

    job._wait = wait
    assert asyncio.run(job._deliver_window())["status"] == "sent"
    assert seen == [{"status": "retry", "error_type": "RuntimeError"}]


def test_disabled_scheduler_does_not_start(monkeypatch):
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: SimpleNamespace(
            get=lambda key, default: default, env=lambda key, default: "false"
        ),
    )
    job = module.MarketDigestScheduler()
    job.start()
    assert job._task is None and job.status()["enabled"] is False


def test_lifespan_starts_and_stops_mail_even_when_quotes_are_disabled(monkeypatch):
    import jobs.lifecycle as lifespan_module
    from tools.auth import users as accounts
    from jobs import zsxq_scheduler

    calls = []

    async def stop():
        calls.append("mail-stop")

    async def noop():
        pass

    monkeypatch.setattr(
        lifespan_module,
        "get_config",
        lambda: SimpleNamespace(
            get=lambda key, default=None: False
            if key in ("schedule.enabled", "zsxq_schedule.enabled")
            else default
        ),
    )
    monkeypatch.setattr(accounts, "ensure_auth_schema", lambda: None)
    monkeypatch.setattr(
        lifespan_module,
        "get_digest_scheduler",
        lambda: SimpleNamespace(start=lambda: calls.append("mail-start"), stop=stop),
    )
    monkeypatch.setattr(
        lifespan_module, "get_scheduler", lambda: SimpleNamespace(stop=noop)
    )
    monkeypatch.setattr(
        lifespan_module,
        "get_intraday_scheduler",
        lambda: SimpleNamespace(start=lambda: None, stop=noop),
    )
    monkeypatch.setattr(lifespan_module, "get_midday_scheduler",
                        lambda: SimpleNamespace(start=lambda: None, stop=noop))
    monkeypatch.setattr(
        zsxq_scheduler, "get_zsxq_scheduler", lambda: SimpleNamespace(stop=noop)
    )

    async def scenario():
        async with lifespan_module.lifespan(SimpleNamespace(state=SimpleNamespace())):
            assert calls == ["mail-start"]

    asyncio.run(scenario())
    assert calls == ["mail-start", "mail-stop"]
