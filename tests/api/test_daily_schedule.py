from datetime import datetime, date, timezone
import asyncio
import pytest
from jobs import scheduler
from tools.market import sync_index


@pytest.fixture(autouse=True)
def isolate_index_sync(monkeypatch):
    monkeypatch.setattr(sync_index, "pending_index_days", lambda target: {})
    monkeypatch.setattr(scheduler, "_run_index_incremental", lambda target: {"status": "complete"})


def test_next_slot_uses_shanghai_not_host_or_utc_clock(monkeypatch):
    monkeypatch.setattr(scheduler, "SYNC_HOUR", 17)
    monkeypatch.setattr(scheduler, "SYNC_MINUTE", 0)
    job = scheduler.DailySyncScheduler()
    before = job._next_run_time(datetime(2026, 11, 12, 8, 59, tzinfo=timezone.utc))
    after = job._next_run_time(datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc))
    assert before.isoformat() == "2026-11-12T17:00:00+08:00"
    assert after.isoformat() == "2026-11-13T17:00:00+08:00"


def test_friday_after_slot_skips_weekend_and_holiday(monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "is_trading_day",
        lambda d: d.weekday() < 5 and d != date(2026, 11, 16),
    )
    job = scheduler.DailySyncScheduler()
    assert job._next_run_time(datetime(2026, 11, 13, 18)).date() == date(2026, 11, 17)


def test_startup_before_17_does_not_claim_today_is_due(monkeypatch):
    monkeypatch.setattr(scheduler, "SYNC_HOUR", 17)
    assert scheduler.DailySyncScheduler._expected_latest_trade_date(
        datetime(2026, 11, 12, 16, 59)
    ) == date(2026, 11, 11)
    assert scheduler.DailySyncScheduler._expected_latest_trade_date(
        datetime(2026, 11, 12, 17, 0)
    ) == date(2026, 11, 12)


def test_startup_database_read_is_off_event_loop(monkeypatch):
    import threading
    from jobs import refresh_market

    main = threading.get_ident()
    seen = []

    def missing(target):
        seen.append(threading.get_ident())
        return False

    monkeypatch.setattr(refresh_market, "needs_refresh", missing)
    monkeypatch.setattr(
        scheduler.DailySyncScheduler,
        "_expected_latest_trade_date",
        staticmethod(lambda: date(2026, 11, 12)),
    )
    asyncio.run(scheduler.DailySyncScheduler()._catchup_if_missed())
    assert seen and seen[0] != main


def test_unexpected_sync_failure_does_not_kill_the_schedule(monkeypatch):
    monkeypatch.setattr("jobs.refresh_market.needs_refresh", lambda target: True)
    job = scheduler.DailySyncScheduler()
    calls = []

    async def skip_catchup():
        pass

    async def fail_then_stop(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary source failure")
        job._stop_event.set()

    job._catchup_if_missed = skip_catchup
    job._seconds_until = lambda target: 0
    job._run_sync = fail_then_stop
    asyncio.run(asyncio.wait_for(job._loop(), timeout=2))
    assert len(calls) == 2


def test_startup_repairs_minutes_or_snapshot_even_when_daily_is_current(monkeypatch):
    from jobs import refresh_market
    monkeypatch.setattr(refresh_market, "needs_refresh", lambda target: True)
    monkeypatch.setattr(scheduler.DailySyncScheduler, "_expected_latest_trade_date",
                        staticmethod(lambda: date(2026, 11, 13)))
    job = scheduler.DailySyncScheduler()
    seen = []

    async def sync(**kwargs):
        seen.append(kwargs)

    job._run_sync = sync
    asyncio.run(job._catchup_if_missed())
    assert seen == [{"include_financial": False, "end": date(2026, 11, 13), "refresh_quotes": True}]


def test_startup_repairs_only_stale_index_and_keeps_retry_armed(monkeypatch):
    target = date(2026, 11, 18)
    monkeypatch.setattr("jobs.refresh_market.needs_refresh", lambda _: False)

    def forbidden(*args, **kwargs):
        pytest.fail("an index gap must not rebuild the stock snapshot")

    monkeypatch.setattr("jobs.refresh_market.refresh", forbidden)
    monkeypatch.setattr(sync_index, "pending_index_days", lambda _: {"000905.SH": [target]})
    monkeypatch.setattr(scheduler, "_run_index_incremental", lambda _: {"status": "partial"})
    monkeypatch.setattr(scheduler.DailySyncScheduler, "_expected_latest_trade_date",
                        staticmethod(lambda: target))
    job = scheduler.DailySyncScheduler()
    asyncio.run(job._catchup_if_missed())
    assert job.last_result["index_daily"]["status"] == "partial"
    assert job._retry_target == target and job._retry_count == 1


def test_retry_is_bounded_and_success_clears_it():
    job = scheduler.DailySyncScheduler()
    target = date(2026, 11, 13)
    for _ in range(3):
        job._record_retry(target, {"status": "error"})
        assert job._retry_at is not None
    job._record_retry(target, {"status": "error"})
    assert job._retry_at is None
    job._record_retry(target, {"status": "complete"})
    assert job._retry_at is None and job._retry_count == 0


def test_manual_trigger_queues_instead_of_waiting_for_provider_io():
    async def scenario():
        job = scheduler.DailySyncScheduler()
        gate = asyncio.Event()
        job._run_sync = gate.wait
        assert await job.trigger_now() == {"status": "queued"}
        assert await job.trigger_now() == {"status": "busy"}
        gate.set()
        await job._manual_task

    asyncio.run(scenario())


def test_market_pipeline_runs_off_loop_before_financials(monkeypatch):
    import threading
    from jobs import refresh_market
    main = threading.get_ident()
    seen = []

    def refresh(target, **kwargs):
        seen.append(("market", threading.get_ident()))
        return {"status": "complete"}

    def finance(*args):
        seen.append(("financial", threading.get_ident()))
        return {}

    monkeypatch.setattr(refresh_market, "refresh", refresh)
    monkeypatch.setattr(scheduler, "_run_financial_incremental", finance)
    monkeypatch.setattr(scheduler, "_run_dividend_daily", lambda *a: 0)
    monkeypatch.setattr(scheduler, "_run_disclosure_daily", lambda *a: 0)
    result = asyncio.run(scheduler.DailySyncScheduler()._run_sync(end=date(2026, 11, 13)))
    assert result["status"] == "complete"
    assert [name for name, thread in seen] == ["market", "financial"]
    assert all(thread != main for name, thread in seen)


def test_failed_market_refresh_does_not_start_slow_financial_tail(monkeypatch):
    from jobs import refresh_market
    monkeypatch.setattr(refresh_market, "refresh", lambda *a, **kw: {"status": "error"})
    def forbidden(*args):
        raise AssertionError("financial work must not delay market retries")
    monkeypatch.setattr(scheduler, "_run_financial_incremental", forbidden)
    monkeypatch.setattr(scheduler, "_run_dividend_daily", forbidden)
    monkeypatch.setattr(scheduler, "_run_disclosure_daily", forbidden)
    job = scheduler.DailySyncScheduler()
    target = date(2026, 11, 18)
    monkeypatch.setattr(job, "_expected_latest_trade_date", lambda: target)
    result = asyncio.run(job._run_sync(end=target))
    assert result["status"] == "error"
    assert job._retry_at is not None and job._retry_count == 1
    assert "financial" not in result


def test_retry_skips_a_pipeline_already_completed_by_another_process(monkeypatch):
    job = scheduler.DailySyncScheduler()
    job._retry_at = scheduler._market_time()

    async def skip():
        pass

    def no_longer_missing(target):
        job._stop_event.set()
        return False

    async def unexpected(**kwargs):
        raise AssertionError("completed CLI work must not rescan")

    monkeypatch.setattr("jobs.refresh_market.needs_refresh", no_longer_missing)
    job._catchup_if_missed = skip
    job._seconds_until = lambda target: 0
    job._run_sync = unexpected
    asyncio.run(asyncio.wait_for(job._loop(), timeout=2))


def test_long_catchup_crossing_today_slot_does_not_skip_today(monkeypatch):
    monkeypatch.setattr("jobs.refresh_market.refresh", lambda *a, **kw: {"status": "complete"})
    monkeypatch.setattr(scheduler.DailySyncScheduler, "_expected_latest_trade_date",
                        staticmethod(lambda: date(2026, 11, 16)))
    job = scheduler.DailySyncScheduler()
    asyncio.run(job._run_sync(include_financial=False, end=date(2026, 11, 13)))
    assert job._retry_at is not None and job._retry_target == date(2026, 11, 16)
    assert job._retry_count == 0
