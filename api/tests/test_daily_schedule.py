from datetime import datetime, date, timezone
import asyncio
from jobs import scheduler


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
    from tools.market import queries

    main = threading.get_ident()
    seen = []

    def latest():
        seen.append(threading.get_ident())
        return date(2026, 11, 12)

    monkeypatch.setattr(queries, "latest_daily_date", latest)
    monkeypatch.setattr(
        scheduler.DailySyncScheduler,
        "_expected_latest_trade_date",
        staticmethod(lambda: date(2026, 11, 12)),
    )
    asyncio.run(scheduler.DailySyncScheduler()._catchup_if_missed())
    assert seen and seen[0] != main


def test_unexpected_sync_failure_does_not_kill_the_schedule():
    job = scheduler.DailySyncScheduler()
    calls = []

    async def skip_catchup():
        pass

    async def fail_then_stop():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary source failure")
        job._stop_event.set()

    job._catchup_if_missed = skip_catchup
    job._seconds_until = lambda target: 0
    job._run_sync = fail_then_stop
    asyncio.run(asyncio.wait_for(job._loop(), timeout=2))
    assert len(calls) == 2
