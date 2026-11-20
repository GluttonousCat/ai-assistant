from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from core import offpeak, offpeak_queue as queue
from core.local_state import read_json
from workflows.reports import fetch, scheduler


@pytest.mark.parametrize("timestamp,eligible", [
    ("2026-11-20T13:59:59", False), ("2026-11-20T14:00", True),
    ("2026-11-20T23:59:59", True), ("2026-11-21T00:00", False),
])
def test_shanghai_window(timestamp, eligible):
    assert offpeak.is_offpeak(datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)) is eligible


def test_noon_restart_catches_up_crawl_once_and_never_starts_models(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "queue_root", lambda: tmp_path)
    svc = scheduler.ZSXQScheduleScheduler()
    run = Mock(return_value={"fetch": {"pg_sync": "ok"}})
    monkeypatch.setattr(svc, "_run_round", run)
    now = offpeak.local_time(datetime(2026, 11, 20, 14, 30))
    slot = scheduler.latest_slot(now, svc.run_hours)
    assert slot.hour == 12
    svc._run_due(slot)
    new_svc = scheduler.ZSXQScheduleScheduler()
    monkeypatch.setattr(new_svc, "_run_round", run)
    new_svc._run_due(slot)
    run.assert_called_once_with(12)
    monkeypatch.setattr(scheduler, "process_pending", lambda: pytest.fail("daytime model work"))
    monkeypatch.setattr(scheduler, "enqueue", lambda *a, **kw: pytest.fail("daytime maintenance"))
    scheduler.OffPeakScheduler()._tick(now)


def test_download_callback_syncs_and_deduplicates(monkeypatch):
    merge = Mock()
    sync = Mock()
    sync.return_value.run.return_value = [42]
    monkeypatch.setattr("workflows.reports.sync.ZSXQ2PGSync", sync)
    monkeypatch.setattr("workflows.reports.deduplicate.merge", merge)
    assert fetch._process_one_report("g") == "ok"
    sync.return_value.run.assert_called_once()
    merge.assert_called_once()


def test_queue_survives_cutoff_and_repeated_submission(monkeypatch, tmp_path):
    monkeypatch.setattr(queue, "queue_root", lambda: tmp_path)
    clock = [False]
    monkeypatch.setattr(offpeak, "is_offpeak", lambda: clock[0])
    key = queue.enqueue("extract", report_id=9)
    assert queue.enqueue("extract", report_id=9) == key
    pending = tmp_path / "pending" / f"{key}.json"
    calls = []
    def handle(report_id):
        calls.append(report_id)
        if len(calls) == 1:
            clock[0] = False
        offpeak.check_model_window()
    assert queue.drain({"extract": handle}) == 0
    assert calls == []
    clock[0] = True
    assert queue.drain({"extract": handle}) == 0
    assert read_json(pending)["attempts"] == 0
    assert not offpeak.check_model_window()  # An interactive call has its own scope.
    clock[0] = True
    assert queue.drain({"extract": handle}) == 1
    assert calls == [9, 9]
    assert not pending.exists()


def test_no_daytime_sdk_call_for_single_report_metadata(monkeypatch):
    from tools.finance import report_meta_analysis as metadata

    client, pg = Mock(), Mock()
    pg.fetch_one.return_value = {"file_name": "研报.pdf"}
    monkeypatch.setattr(metadata, "ensure_meta_columns", lambda: None)
    monkeypatch.setattr(metadata, "get_config", lambda: Mock(openai_api_key=True))
    monkeypatch.setattr(metadata, "_get_llm", lambda: (client, "model"))
    with monkeypatch.context() as m:
        m.setattr(offpeak, "is_offpeak", lambda: True)
        with offpeak.background_models():
            m.setattr(offpeak, "is_offpeak", lambda: False)
            with pytest.raises(offpeak.OffPeakEnded):
                metadata.analyze_report_meta(42, pg=pg)
    assert not client.mock_calls
    pg.execute.assert_not_called()
