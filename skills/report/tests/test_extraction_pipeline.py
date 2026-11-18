import json

import pytest

from skills.base import SkillContext
from skills.report.skill import ReportSkill
from storage.repositories import report_analysis as repo


@pytest.fixture
def pipeline(monkeypatch):
    row = dict(
        report_id=1,
        title="公司研究",
        content_text="正文" * 300,
        content_chars=600,
        file_path=None,
        analysis_json=None,
    )
    state = dict(active=0, rows=[row], sql=[], transactions=[], deleted=False)

    class Pg:
        def __enter__(self):
            state["active"] += 1
            return self

        def __exit__(self, error, *_):
            state["active"] -= 1
            state["transactions"].append("rollback" if error else "commit")

        def fetch_one(self, sql, args):
            return None if state["deleted"] else state["rows"][0]

        def fetch_all(self, sql, args):
            return state["rows"]

        def execute(self, sql, args):
            state["sql"].append((sql, args))
            if "UPDATE" in sql and state["deleted"]:
                return 0
            if "INSERT" in sql and state.get("fail_insert"):
                raise RuntimeError("forecast insert failed")
            return 1

    monkeypatch.setattr(repo, "PgClient", lambda **kwargs: Pg())
    monkeypatch.setattr(repo, "ensure_report_analysis_columns", lambda: None)
    skill = ReportSkill()
    background = []

    def after(rid):
        assert state["active"] == 0
        assert state["transactions"][-1] == "commit"
        background.append(rid)

    monkeypatch.setattr(skill, "_after_extract", after)
    data = {
        "market": "A股",
        "ts_codes": ["600519.SH"],
        "rating": "买入",
        "forecasts": [
            {
                "symbol": "600519.SH",
                "metric": "net_profit",
                "period": "2026E",
                "value": 900,
                "unit": "亿元",
            }
        ],
    }

    class LLM:
        def invoke(self, prompt):
            assert state["active"] == 0, (
                "Model calls must not hold database connections"
            )
            return state.get("raw", json.dumps(data))

        def stream(self, prompt):
            for char in self.invoke(prompt):
                assert state["active"] == 0
                yield char

    monkeypatch.setattr(skill, "_extract_llm", lambda: LLM())
    return skill, state, background, data


def test_stream_does_not_hold_pg_and_continuations_follow_commit(pipeline):
    skill, state, background, _ = pipeline
    events = list(skill.extract_stream(1))
    assert events[-1]["type"] == "done"
    data = next(e["data"] for e in events if e["type"] == "data")
    assert data["forecast_count"] == 1
    assert background == [1]
    assert state["active"] == 0
    assert any("INSERT INTO fin.report_forecast" in sql for sql, _ in state["sql"])


def test_missing_record_never_inserts_forecasts_or_starts_followup(pipeline):
    skill, state, background, _ = pipeline
    state["deleted"] = True
    assert skill._extract_one(state["rows"][0]) is None
    assert not background
    assert not any("INSERT" in sql for sql, _ in state["sql"])


@pytest.mark.parametrize("raw", ["{broken}", "[1,2]", '"text"', "null"])
def test_invalid_or_non_object_model_output_is_failed_not_success(pipeline, raw):
    skill, state, background, _ = pipeline
    state["raw"] = raw
    context = SkillContext()
    skill._run_extract(context, {})
    assert context.result["processed"] == []
    assert context.result["failed"] == [1]
    assert not background


def test_batch_summary_separates_completed_ocr_and_missing_text(pipeline, monkeypatch):
    skill, state, background, _ = pipeline
    state["rows"] += [
        dict(report_id=2, content_chars=0, file_path="scan.pdf"),
        dict(report_id=3, content_chars=0),
    ]
    monkeypatch.setattr(skill, "_vision_backfill", lambda row: False)
    context = SkillContext()
    skill._run_extract(context, {})
    assert context.result["processed"] == [1]
    assert context.result["queued_ocr"] == [2]
    assert context.result["failed"] == [3]
    assert background == [1]


def test_empty_forecasts_replace_old_rows_and_insert_failure_rolls_back(pipeline):
    skill, state, background, data = pipeline
    data["forecasts"] = []
    assert skill._apply_extract_result(state["rows"][0], data)["forecast_count"] == 0
    assert any("DELETE FROM fin.report_forecast" in sql for sql, _ in state["sql"])
    data["forecasts"] = [
        {
            "symbol": "600519.SH",
            "metric": "eps",
            "period": "2026E",
            "value": 4,
            "unit": "元",
        }
    ]
    state["fail_insert"] = True
    with pytest.raises(RuntimeError, match="insert failed"):
        skill._apply_extract_result(state["rows"][0], data)
    assert state["transactions"][-1] == "rollback"
    assert not background
