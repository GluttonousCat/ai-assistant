from decimal import Decimal

from harness.mcp.tools import reports
from skills.beta_alpha.analysis.alpha_context import report_views
from skills.content.profile import profile_digest
from skills.report.normalization import prepare_extraction
from skills.research.sources.report_source import build_views_from_report


def test_report_model_receives_excerpt_and_page_coverage_before_legacy_analysis(monkeypatch):
    import json
    from harness.execution import model_tool_result
    from skills.report.document import Document, Segment

    row = {"report_id": 5, "title": "材料展望", "org_name": "甲机构",
           "content_text": "", "analysis_json": json.dumps({"old": "x" * 5000})}
    monkeypatch.setattr(reports.report_catalog, "report_body", lambda _: row)
    monkeypatch.setattr(reports, "load_document", lambda _: Document(
        [Segment(1, 2, 'Original "outlook"\n' * 400)], "pdf_ocr", 22, (1, *range(3, 23))))
    data = reports.read_report(5)
    message = model_tool_result({"ok": True, "tool": "read_report", "data": data}, 4000)
    result = json.loads(message)["data"]
    assert len(message) <= 4000 and result["truncated"]
    assert (result["pages_with_text"], result["page_count"], result["body_status"]) == (1, 22, "partial")
    assert "PDF 第 2 页" in result["content_excerpt"] and 'Original "outlook"' in result["content_excerpt"]


def test_pending_attachment_has_no_quotable_excerpt(monkeypatch):
    row = {"report_id": 1, "title": "某展望", "org_name": "", "source": "zsxq",
           "content_text": "发帖人解读", "extraction_status": "pending"}
    monkeypatch.setattr(reports.report_catalog, "report_body", lambda _: row)
    response = reports.read_report(1)
    assert response["body_status"] == "awaiting_attachment"
    assert response["content_excerpt"] == ""


def test_commentary_attribution_survives_search_alpha_and_profile():
    row = {
        "report_id": 9, "title": "盈利前景", "org_name": "甲机构", "publish_date": "2026-11-18",
        "analysis_json": '{"material_type":"commentary","rating":"买入","core_view":"需求回升"}',
    }
    brief = reports._brief(row)
    assert brief["material_type"] == "commentary"
    view, = report_views([row])
    assert (view["material_type"], view["core_view"]) == ("commentary", "需求回升")
    digest = profile_digest({"sections": {"reports": {"reports": [brief]}}})
    assert "二手解读（材料提及机构：甲机构）" in digest
    assert reports._brief({**row, "analysis_json": None})["material_type"] == "unknown"


def test_sell_side_summary_keeps_separate_units(monkeypatch):
    monkeypatch.setattr(reports, "resolve_one", lambda _: {"ts_code": "600001.SH", "name": "甲公司"})
    row = {"org_name": "乙机构", "title": "", "publish_date": None,
           "forecast_type": "net_profit", "forecast_period": "2027"}
    rows = [{**row, "forecast_value": Decimal(value), "forecast_unit": unit}
            for value, unit in [("1", "亿元"), ("2", "亿元"), ("15000", "万元"), ("4", None)]]
    monkeypatch.setattr(reports.finance_data, "sell_side_forecasts", lambda *_: rows)
    result = reports.summarize_sell_side("甲公司")
    stats = {s["unit"]: s for s in result["forecast_summary"]}
    assert set(stats) == {"亿元", "万元"}
    assert (stats["亿元"]["n_forecasts"], stats["亿元"]["mean"]) == (2, 1.5)
    assert stats["万元"]["mean"] == 15000
    assert len(result["latest_detail"]) == 4


def test_speaker_conditions_and_quotes_survive_all_report_consumers():
    import json

    source = {"segment": 1, "page": 3, "quote": "Respondents expect a recovery only if inventories fall."}
    statement = "受访者表示需求可能恢复，" + "具体时点和客户补货进度仍有较大不确定性，" * 8 + "前提是库存下降。"
    note = {"statement": statement, "attribution": "survey", "stance": "conditional", "source": source}
    data = {"analysis_version": 4, "material_type": "original", "rating": "买入", "core_view": "",
            "key_points": ["调查受访者：" + statement], "risks": [],
            "summary_evidence": {"core_view": None, "key_points": [note], "risks": []}}
    fields, _, sse = prepare_extraction({"report_id": 7, "title": "情况调查"}, data)
    assert sse["rating"] == "无"
    assert sse["key_points"] == data["key_points"]
    assert sse["summary_evidence"] == json.loads(fields["analysis_json"])["summary_evidence"]
    row = {"report_id": 7, "title": "情况调查", "org_name": "甲机构", "publish_date": "2026-11-18",
           "analysis_json": fields["analysis_json"]}
    brief = reports._brief(row)
    assert brief["lead_evidence"] == note and brief["rating_source"] is None
    alpha, = report_views([row])
    assert not alpha["core_view"] and alpha["lead_evidence"] == note
    digest = profile_digest({"sections": {"reports": {"reports": [brief]}}})
    view, = build_views_from_report({"analysis": data}, brief)
    for text in (digest, view.statement):
        assert "调查" in text and "条件判断" in text and "前提是库存下降" in text and "第3页" in text


def test_only_original_broker_baseline_enters_forecast_statistics():
    source = {"segment": 3, "page": 2, "quote": "Our base case EPS estimate is 3.50."}
    forecast = {"symbol": "600519.SH", "metric": "eps", "period": "2027E", "value": 3.5, "unit": "元",
                "attribution": "broker", "stance": "expectation", "source": source,
                "raw_text": "保证增长90%、没有风险。"}
    forecasts = [forecast,
                 {**forecast, "attribution": "management", "value": 3.4,
                  "source": {**source, "quote": "Management guidance is 3.40."}},
                 {**forecast, "stance": "conditional", "value": 3.6,
                  "source": {**source, "quote": "If utilization improves, EPS could reach 3.60."}}]
    data = {"analysis_version": 4, "material_type": "original", "forecasts": forecasts}
    for material, count in (("original", 1), ("unknown", 0), ("commentary", 0)):
        fields, broker, sse = prepare_extraction({"report_id": 3}, {**data, "material_type": material})
        assert len(broker) == count
        assert sse["forecast_count"] == 3
        assert [f["attribution"] for f in sse["forecasts"]] == ["broker", "management", "broker"]
        assert sse["forecasts"][0]["raw_text"] == "2027E 每股收益 3.5元"
        assert sse["forecasts"][0]["confidence"] is None
