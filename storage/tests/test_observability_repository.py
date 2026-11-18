from types import SimpleNamespace

import pytest

from core.local_state import write_json
from storage.repositories import observability as store


def test_log_tail_is_bounded_filtered_redacted_and_rotation_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "log_directory", lambda: tmp_path)
    monkeypatch.setenv("TEST_TOKEN", "private-token-value")
    path = tmp_path / "jobs.log"
    path.write_text(
        "old line\n" * 40000 + "2026-11-18 - jobs.test - INFO - completed 600000.SH\n"
        "2026-11-18 - jobs.test - ERROR - password='sensitive'\n"
        "  traceback private-token-value\n",
        encoding="utf-8",
    )
    result = store.log_tail(limit=2)
    assert result["truncated"] and len(result["lines"]) == 2
    assert "sensitive" not in str(result) and "private-token-value" not in str(result)
    assert all(r["level"] == "ERROR" for r in result["lines"])
    assert len(store.log_tail(level="INFO", search="600000")["lines"]) == 1
    path.rename(tmp_path / "jobs.log.2026-11-17")
    assert store.log_tail()["lines"] == []
    path.write_text("new file", encoding="utf-8")
    assert store.log_tail()["lines"][0]["text"] == "new file"


@pytest.mark.parametrize(
    "source,level", [("../.env", "ALL"), ("jobs.log", "ALL"), ("jobs", "TRACE")]
)
def test_log_sources_are_allowlisted(source, level):
    with pytest.raises(ValueError):
        store.log_tail(source=source, level=level)


def test_queue_summary_does_not_expose_owner_or_full_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "get_config", lambda: SimpleNamespace(root_dir=tmp_path))
    monkeypatch.setattr(store, "runs_root", lambda: tmp_path / "runs")
    write_json(
        tmp_path / "output/quant/workspace/tasks/a.json",
        {
            "id": "a",
            "owner": "42",
            "spec": {"private": "strategy"},
            "request": {"private": "request"},
            "status": "queued",
            "progress": "等待运行",
            "created_at": "2026-11-18T00:00:00+00:00",
            "updated_at": "2026-11-18T00:00:00+00:00",
        },
    )
    row = store.task_snapshot()["active"][0]
    assert row["kind"] == "quant" and not ({"owner", "request", "spec"} & row.keys())
