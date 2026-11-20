import json

from skills.report.document import Document, Segment, load_document, qa_context
from skills.report.extraction import analyze_document
from skills.report.normalization import prepare_extraction


def test_commentary_forecasts_remain_readable_without_entering_broker_series():
    cover = "券商年报解读\n公众号：猫哥读研报"
    quote = "材料转述，公司预计2027年每股收益为3.5元。"
    doc = Document([Segment(1, 1, cover), Segment(2, 2, quote)], "pdf_text", 2)

    class LLM:
        def invoke(self, _):
            return json.dumps({
                "market": "A股", "ts_codes": ["600519.SH"],
                "material": {"type": "original", "source": {"segment": 1, "quote": cover}},
                "forecasts": [{
                    "symbol": "600519.SH", "metric": "eps", "period": "2027E",
                    "value": 3.5, "unit": "元", "source": {"segment": 2, "quote": quote},
                }],
            })

    data = list(analyze_document(doc, "年度展望", LLM()))[-1]["data"]
    fields, broker_forecasts, summary = prepare_extraction({"report_id": 7}, data)
    assert summary["material_type"] == "commentary"
    assert summary["forecast_count"] == 1 and not broker_forecasts
    stored = json.loads(fields["analysis_json"])
    assert stored["material_source"]["quote"] == cover
    assert stored["forecasts"][0]["source"] == {"segment": 2, "page": 2, "quote": quote}


def test_material_attribution_needs_a_matching_first_page_quote():
    doc = Document([Segment(1, 2, "BofA GLOBAL RESEARCH\n知识星球：示例研报群")], "pdf_ocr", 3, (1, 3))

    class LLM:
        def invoke(self, _):
            return json.dumps({"core_view": "观点", "material": {
                "type": "original", "source": {"segment": 1, "quote": "BofA GLOBAL RESEARCH"},
            }})

    data = list(analyze_document(doc, "研究报告", LLM()))[-1]["data"]
    assert data["material_type"] == "unknown" and data["material_source"] is None


def test_chinese_qa_retrieves_current_original_quote_and_ignores_stale_index():
    quote = "Management expects stockpiles to decline through June."
    segments = [Segment(n, None, "unrelated filler " * 130) for n in range(1, 10)]
    segments[-1] = Segment(9, None, quote)
    document = Document(segments, "stored_text")
    question = "库存什么时候下降"
    valid = {"statement": "预计库存在六月前下降", "source": {"segment": 9, "quote": quote}}
    stale = {**valid, "source": {"segment": 8, "quote": quote}}
    selected = qa_context(document, question, {"evidence": [stale, valid]})
    assert any(p.number == 9 and quote in p.text for p in selected)
    assert not any(p.number == 8 for p in selected)


def test_archived_attachment_with_stale_path_requires_a_traceable_source(tmp_path):
    row = {"file_path": str(tmp_path / "missing.pdf"), "source": "zsxq_file",
           "extraction_status": "extracted", "content_text": "旧附件可能保存的话题文字"}
    assert load_document(row).segments == []
    row["content_text"] = "[[PDF_PAGE:2]]\n旧附件可定位的 OCR 正文"
    segment, = load_document(row).segments
    assert segment.page == 2


def test_synthesis_passes_report_identity_and_verified_conditions(monkeypatch):
    from skills.base import SkillContext
    from skills.report.skill import ReportSkill
    from tools.finance import report_analysis

    insight = {"statement": "供应恢复速度决定过剩时点", "indicator": "冶炼厂复产",
               "source": {"segment": 4, "page": 3, "quote": "Watch smelter restarts."}}
    analysis = {"material_type": "commentary", "research_insights": {"watchpoints": [insight]}}
    rows = [{"report_id": 9, "title": "铝市场", "analysis_json": json.dumps(analysis)},
            {"report_id": 8, "title": "历史研报", "analysis_json": '{"core_view":"供应收紧"}'}]
    monkeypatch.setattr(report_analysis, "matched_reports", lambda _: rows)
    skill = ReportSkill()

    class LLM:
        def invoke(self, prompt):
            assert "Watch smelter restarts." in prompt and "供应收紧" not in prompt
            assert '"material_type": "commentary"' in prompt and '"report_id": 9' in prompt
            claim = {"report_id": 9, "statement": "关注冶炼厂复产。", "attribution": "third_party",
                     "stance": "observation", "source": {**insight["source"], "page": 99}}
            return json.dumps({"claims": [claim, {**claim, "report_id": 8}, {**claim, "report_id": "9"}]})

    monkeypatch.setattr(skill, "_llm", LLM)
    context = SkillContext(params={"topic": "铝"})
    skill._run_analyze(context, context.params)
    assert context.result["report_count"] == 2
    assert context.result["reports"][1]["material_type"] == "unknown"
    claim, = context.result["citations"]
    assert claim["report_id"] == 9 and claim["source"]["page"] == 3
    assert "二手材料转述" in context.result["summary"] and "Watch smelter restarts." in context.result["summary"]
