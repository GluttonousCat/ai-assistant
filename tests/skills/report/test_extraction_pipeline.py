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
        content_text="报告预计2026年净利润900亿元。\n" + "正文" * 300,
        content_chars=622,
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
            if not state["deleted"]:
                return next((r for r in state["rows"] if r["report_id"] == args[0]), None)

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
                "source": {"segment": 1, "quote": "报告预计2026年净利润900亿元。"},
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


@pytest.mark.parametrize("streaming", [False, True])
def test_verified_insight_survives_save_and_stream(pipeline, streaming):
    skill, state, _, model = pipeline
    model["evidence"] = [{
        "kind": "driver", "insight_type": "theses", "statement": "预计净利润900亿元。",
        "source": {"segment": 1, "quote": "报告预计2026年净利润900亿元。"},
    }]
    if streaming:
        events = list(skill.extract_stream(1))
        summary = next(event["data"] for event in events if event["type"] == "data")
    else:
        summary = skill._extract_one(state["rows"][0])
    sql, params = next((sql, args) for sql, args in state["sql"] if sql.startswith("UPDATE"))
    stored = json.loads(params[0])
    assert stored["research_insights"] == summary["research_insights"]
    assert stored["research_insights"]["theses"][0]["source"] == {
        "segment": 1, "quote": "报告预计2026年净利润900亿元。", "page": None,
    }
    assert "content_text IS NOT DISTINCT FROM %s" in sql
    assert params[-1] == state["rows"][0]["content_text"]


@pytest.mark.parametrize("raw", ["{broken}", "[1,2]", '"text"', "null"])
def test_invalid_or_non_object_model_output_is_failed_not_success(pipeline, raw):
    skill, state, background, _ = pipeline
    state["raw"] = raw
    context = SkillContext()
    skill._run_extract(context, {})
    assert context.result["processed"] == []
    assert context.result["failed"] == [1]
    assert not background


def test_metadata_only_retry_preserves_previous_analysis(pipeline):
    skill, state, background, _ = pipeline
    state["rows"][0]["analysis_json"] = '{"core_view":"原有解读"}'
    state["raw"] = '{"market":"A股","forecasts":[],"key_points":[],"evidence":[]}'
    events = list(skill.extract_stream(1))
    assert events[-1]["type"] == "error" and "原有分析已保留" in events[-1]["message"]
    assert not background
    assert not any("DELETE" in sql or "analysis_json=%s" in sql for sql, _ in state["sql"])


def test_batch_summary_separates_completed_ocr_and_missing_text(pipeline, monkeypatch, tmp_path):
    skill, state, background, _ = pipeline
    scan = tmp_path / "scan.pdf"
    scan.touch()
    monkeypatch.setattr("tools.finance.report_extractor.ReportExtractor.pdf_pages", lambda *args, **kwargs: [""])
    state["rows"] += [
        dict(report_id=2, content_chars=0, file_path=str(scan)),
        dict(report_id=3, content_chars=0, file_path=str(tmp_path / "missing.pdf")),
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


def test_later_pages_keep_grounded_forecasts_across_synthesis(pipeline, monkeypatch):
    skill, state, _, _ = pipeline
    from skills.report.document import Document, Segment

    row = state["rows"][0]
    pages = [Segment(i, i, f"第{i}页正文 " + "abcdefgh " * 840) for i in range(1, 6)]
    quote = "2027E 净利润 | 1,234.5 亿元"
    pages.append(Segment(6, 6, quote))
    monkeypatch.setattr(
        "skills.report.skill.load_document", lambda _: Document(pages, "pdf_text", 6)
    )
    forecast = {
        "symbol": "600519.SH", "metric": "net_profit",
        "period": "2027E", "value": 1234.5, "unit": "亿元",
        "source": {"segment": 6, "page": 1, "quote": quote},
    }

    class LLM:
        def invoke(self, prompt):
            assert state["active"] == 0
            data = {"core_view": "分段观点", "market": "A股"}
            if quote in prompt:
                data["forecasts"] = [
                    forecast, {**forecast, "value": 4321},
                    {**forecast, "source": {"segment": 6, "quote": "这句文字并未出现在正文"}},
                ]
            return json.dumps(data)

        def stream(self, prompt):
            assert "合成一份" in prompt
            yield json.dumps({"core_view": "合并观点", "market": "A股"})

    monkeypatch.setattr(skill, "_extract_llm", LLM)
    events = list(skill.extract_stream(row["report_id"]))
    result = next(e["data"] for e in events if e["type"] == "data")
    assert result["core_view"] == "合并观点"
    assert result["forecasts"][0]["source"] == {"segment": 6, "page": 6, "quote": quote}
    assert result["forecast_count"] == 1
    assert result["coverage"]["analyzed_segments"] == 6
    assert result["coverage"]["rejected_forecasts"] == 2
    assert not result["coverage"]["partial"]


def test_failed_later_batch_preserves_previously_saved_report(pipeline, monkeypatch):
    skill, state, background, _ = pipeline
    from skills.report.document import Document, Segment

    row = state["rows"][0]
    row["analysis_json"] = '{"core_view":"已保存观点"}'
    document = Document([Segment(i, i, "正文" * 3999) for i in range(1, 5)], "pdf_text", 4)
    outputs = iter(['{"core_view":"第一页"}', 'null'])
    monkeypatch.setattr(
        skill, "_extract_llm", lambda: type("LLM", (), {"invoke": lambda _, prompt: next(outputs)})()
    )
    assert skill._extract_one(row, document) is None
    assert not background
    assert len(state["sql"]) == 1
    assert "analysis_status IS DISTINCT FROM 'done'" in state["sql"][0][0]


def test_conflicting_forecast_values_are_not_written(pipeline):
    skill, state, _, data = pipeline
    data["coverage"] = {}
    forecast, = data["forecasts"]
    data["forecasts"] = [forecast, forecast.copy(), {**forecast, "value": 901}]
    result = skill._apply_extract_result(state["rows"][0], data)
    assert result["forecasts"] == []
    assert result["coverage"]["forecast_conflicts"] == 1


def test_single_report_retrieval_reaches_later_passages():
    from skills.report.document import Document, Segment, qa_context

    text = "无关 " * 2400
    doc = Document([
        Segment(1, 1, text), Segment(2, 2, text),
        Segment(3, 3, text + "净息差压力测试按三个情景披露。"),
    ], "pdf_text", 3)
    passages = qa_context(doc, "净息差压力测试有什么情景？")
    assert any(p.page == 3 and "净息差" in p.text for p in passages)


def test_ocr_page_markers_preserve_gaps_and_physical_page_numbers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from skills.report.document import load_document

    path = tmp_path / "scan.pdf"
    path.touch()
    monkeypatch.setattr(
        "skills.report.document.get_extractor",
        lambda: SimpleNamespace(pdf_pages=lambda path, layout: [""] * 12),
    )
    document = load_document({
        "file_path": str(path),
        "content_text": "[[PDF_PAGE:1]]\n首页正文\n[[PDF_PAGE:3]]\n第三页正文",
    })
    assert document.source == "pdf_ocr"
    assert document.page_count == 12
    assert [segment.page for segment in document.segments] == [1, 3]
    assert document.missing_pages == (2, *range(4, 13))
