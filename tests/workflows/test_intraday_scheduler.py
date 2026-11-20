import asyncio
from datetime import datetime, timezone
import threading

import pytest

from core.local_state import read_json, write_json
from jobs import intraday_scheduler as m
from jobs.cache_intraday_prefix import session_manifest
from storage.repositories import intraday_watch as repo


def at(hour, minute=0, day=17):
    return datetime(2026, 11, day, hour, minute, tzinfo=m.SHANGHAI)


@pytest.mark.parametrize("hour,minute,expected", [(10, 31, None), (10, 32, "10:30"), (11, 31, "10:30"), (11, 32, "11:30"), (14, 1, "11:30"), (14, 2, "14:00"), (15, 2, "15:00"), (22, 0, "15:00")])
def test_only_settled_closed_slots_are_due(hour, minute, expected):
    assert m.due_slot(at(hour, minute)) == expected


def test_holidays_weekends_and_utc_clock():
    assert m.due_slot(at(15, 10, 14)) is None
    assert m.due_slot(datetime(2026, 10, 1, 15)) is None
    assert m.next_slot(at(15, 2, 13)) == at(10, 32, 16)
    assert m.due_slot(datetime(2026, 11, 17, 6, 2, tzinfo=timezone.utc)) == "14:00"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "root", lambda: tmp_path)
    monkeypatch.setattr(repo, "root", lambda: tmp_path)
    monkeypatch.setattr(m, "market_now", lambda: at(15, 20))
    return tmp_path


def test_late_start_recovers_only_latest_slot_and_restart_does_not_repeat(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "collect", lambda day, slot, **kw: calls.append((day, slot)))

    def scan(day, slot):
        write_json(isolated / "2026-11-17_1500" / "report.json", {"as_of": f"{day} {slot}:00"})
        return {"candidates": 4}

    monkeypatch.setattr(m, "scan", scan)
    job = m.IntradayScheduler()
    assert job.tick(at(15, 20))["status"] == "complete"
    assert m.IntradayScheduler().tick(at(15, 30))["status"] == "complete"
    assert calls == [("2026-11-17", "15:00")]


def test_cross_process_lock_prevents_overlapping_pipelines(isolated, monkeypatch):
    monkeypatch.setattr(m, "collect", lambda *a, **kw: pytest.fail("must not overlap"))
    with m.file_lock(isolated / "scheduler.lock") as acquired:
        assert acquired
        assert m.IntradayScheduler().tick(at(15, 20))["status"] == "busy"


def test_four_sessions_publish_once_each_and_keep_archives(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "collect", lambda day, slot, **kw: calls.append(slot))

    def scan(day, slot):
        run_id = f"{day}_{slot.replace(':', '')}"
        report = {"run_id": run_id, "as_of": f"{day} {slot}:00", "data": []}
        write_json(isolated / run_id / "report.json", report)
        repo.publish_report(report)
        return {"candidates": 0}

    monkeypatch.setattr(m, "scan", scan)
    for hour, minute in ((10, 32), (11, 32), (14, 2), (15, 2)):
        assert m.IntradayScheduler().tick(at(hour, minute))["status"] == "complete"
        assert m.IntradayScheduler().tick(at(hour, minute))["status"] == "complete"
    assert calls == list(m.SLOTS)
    assert repo.read_report()["run_id"] == "2026-11-17_1500"
    assert repo.read_report("2026-11-17_1030")["as_of"] == "2026-11-17 10:30:00"


def test_new_slot_preempts_retry_of_a_missed_earlier_slot(isolated, monkeypatch):
    calls = []

    def fail(day, slot, **kw):
        calls.append(slot)
        raise RuntimeError("temporary")

    monkeypatch.setattr(m, "collect", fail)
    job = m.IntradayScheduler()
    assert job.tick(at(10, 32))["status"] == "error"
    assert job.tick(at(11, 32))["run_id"] == "2026-11-17_1130"
    assert calls == ["10:30", "11:30"]


def test_a_crash_after_archive_write_recovers_latest_and_receipt(isolated, monkeypatch):
    folder = isolated / "2026-11-17_1500"
    write_json(folder / "report.json", {"as_of": "2026-11-17 15:00:00"})
    write_json(folder / "scheduled.json", {"status": "scanning", "attempts": 1})
    monkeypatch.setattr(m, "collect", lambda *a, **kw: pytest.fail("reuse completed archive"))
    assert m.IntradayScheduler().tick(at(15, 20))["status"] == "complete"
    assert repo.read_report()["as_of"] == "2026-11-17 15:00:00"
    assert read_json(isolated / "scheduler.json")["status"] == "complete"


def test_retry_limit_is_durable_and_waits_five_minutes(isolated, monkeypatch):
    calls = []
    clock = [at(15, 20)]
    monkeypatch.setattr(m, "market_now", lambda: clock[0])

    def fail(*args, **kw):
        calls.append(True)
        raise RuntimeError("private database connection")

    monkeypatch.setattr(m, "collect", fail)
    job = m.IntradayScheduler()
    first = job.tick(at(15, 20))
    assert first["error_type"] == "RuntimeError" and "private" not in str(first)
    assert job.tick(at(15, 21))["status"] == "retry_wait"
    clock[0] = at(15, 25)
    assert job.tick(at(15, 25))["attempts"] == 2
    assert m.IntradayScheduler().tick(at(15, 26))["status"] == "retry_wait"
    clock[0] = at(15, 30)
    assert m.IntradayScheduler().tick(at(15, 30))["attempts"] == 3
    assert job.tick(at(15, 35))["status"] == "exhausted"
    assert len(calls) == 3


def test_work_runs_off_event_loop_and_stop_prevents_reentry(monkeypatch):
    main = threading.get_ident()
    threads = []
    job = m.IntradayScheduler()

    def tick():
        threads.append(threading.get_ident())
        job._stop.set()
        return {"status": "complete"}

    monkeypatch.setattr(job, "tick", tick)
    asyncio.run(job._loop())
    assert len(threads) == 1 and threads[0] != main


def test_publishing_an_older_slot_does_not_replace_latest(isolated):
    repo.publish_report({"as_of": "2026-11-17 15:00:00"})
    repo.publish_report({"as_of": "2026-11-17 11:30:00"})
    assert repo.read_report()["as_of"] == "2026-11-17 15:00:00"


def test_publication_check_reads_unchanged_reports_once_and_observes_replacements(isolated, monkeypatch):
    run_id = "2026-11-17_1500"
    cutoff = "2026-11-17 15:00:00"
    archive = isolated / run_id / "report.json"
    latest = isolated / "latest.json"
    write_json(archive, {"as_of": cutoff})
    write_json(latest, {"as_of": cutoff})
    reads = []

    def tracked_read(path, **kwargs):
        reads.append(path)
        return read_json(path, **kwargs)

    monkeypatch.setattr(repo, "read_json", tracked_read)
    assert repo.reconcile_publication(run_id, cutoff)
    assert repo.reconcile_publication(run_id, cutoff)
    assert reads == [archive, latest]
    write_json(archive, {"as_of": "2026-11-17 14:00:00"})
    assert not repo.reconcile_publication(run_id, cutoff)
    write_json(archive, {"as_of": cutoff})
    latest.unlink()
    assert repo.reconcile_publication(run_id, cutoff)
    assert read_json(latest)["as_of"] == cutoff


def test_all_four_slots_keep_the_morning_source_even_after_eod_refresh(tmp_path, monkeypatch):
    from jobs import cache_intraday_prefix as collector
    seed = {"day": "2026-11-17", "snapshot_run_id": "prior", "universe": [{"ts_code": "600000.SH"}]}
    write_json(tmp_path / "2026-11-17_1130" / "collection.json", seed)
    monkeypatch.setattr(collector, "load_dashboard", lambda run, rows: {"as_of": "2026-11-16 15:00:00", "metadata": {}})
    assert session_manifest(tmp_path, "2026-11-17") == seed
    monkeypatch.setattr(collector, "load_dashboard", lambda *a: pytest.fail("reuse pinned day manifest"))
    assert session_manifest(tmp_path, "2026-11-17") == seed


def test_after_eod_start_selects_prior_daily_snapshot(tmp_path, monkeypatch):
    from jobs import cache_intraday_prefix as collector
    seen = []
    monkeypatch.setattr(collector, "tracking_days", lambda end: seen.append(end) or [{"run_id": "prior"}])
    monkeypatch.setattr(collector, "load_dashboard", lambda *args: {"as_of": "2026-11-16 15:00:00", "metadata": {}})
    monkeypatch.setattr(collector, "scan_universe", lambda *a, **kw: [{"ts_code": "600000.SH", "name": "正常"}, {"name": "*ST某某"}])
    manifest = session_manifest(tmp_path, "2026-11-17")
    assert manifest["snapshot_run_id"] == "prior" and len(manifest["universe"]) == 1
    assert seen == ["2026-11-16 23:59:59"]


def test_scan_requires_full_universe_and_successful_followup_before_publish(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from workflows.market import scan_intraday as scan

    folder = tmp_path / "output/market/intraday_watch/2026-11-17_1130"
    member = {"ts_code": "600000.SH", "name": "标的"}
    manifest = {"limited": True, "snapshot_run_id": "prior", "universe": [member]}
    write_json(folder / "collection.json", manifest)
    write_json(folder / "audit.json", {"total": 1, "complete": 1, "failed": []})
    monkeypatch.setattr(scan, "get_config", lambda: SimpleNamespace(root_dir=tmp_path))
    with pytest.raises(ValueError, match="限量"):
        scan.run.__wrapped__("2026-11-17")
    write_json(folder / "collection.json", {**manifest, "limited": False})
    monkeypatch.setattr(scan, "load_dashboard", lambda *a: {"metadata": {"start": "2026-01-01", "config": {}},
                                                            "run_id": "prior", "as_of": "2026-11-16 15:00:00"})
    monkeypatch.setattr(scan, "batch", lambda *a: [{**member, "status": "filtered"}])
    monkeypatch.setattr(scan, "intraday_report", lambda run_id: None)
    monkeypatch.setattr(scan, "publish_report", lambda *a: pytest.fail("unreviewed report published"))

    def unavailable(report):
        raise ConnectionError("backend unavailable")

    monkeypatch.setattr(scan, "build_followup", unavailable)
    with pytest.raises(RuntimeError, match="历史信号审核未完成"):
        scan.run.__wrapped__("2026-11-17")
    progress = read_json(folder / "analysis_progress.json")
    assert progress["status"] == "followup_pending" and progress["error_type"] == "ConnectionError"
    assert not (folder / "report.json").exists()
