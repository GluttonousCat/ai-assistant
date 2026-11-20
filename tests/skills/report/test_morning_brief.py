from datetime import date, datetime, timedelta, timezone
import json

from skills.report import morning_brief as brief
from skills.report.document import Document, Segment


def test_edition_window_is_fixed_on_a_weekend_and_empty_is_valid(monkeypatch):
    windows = []

    def rows(start, end):
        windows.append((start, end))
        return []

    monkeypatch.setattr(brief, "reports_ingested_between", rows)
    issue = brief.load_morning_brief(date(2026, 11, 22))
    assert issue["total_count"] == 0 and issue["reports"] == []
    start, end = windows[0]
    assert start == datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc)
    assert end == start + timedelta(days=1)


def test_keep_lead_and_risk_with_quotes_and_only_use_direct_speaker_for_original(monkeypatch):
    view = {"statement": "如果需求复苏，利润率可能上升。", "attribution": "broker", "stance": "conditional",
            "source": {"quote": "If demand recovers, margins may rise.", "page": 3, "segment": 4}}
    risk = {"statement": "提价存在客户流失风险。", "kind": "risk", "attribution": "management", "stance": "risk",
            "source": {"quote": "提价存在客户流失风险。", "page": 4, "segment": 5}}
    analysis = {"analysis_version": 4, "coverage": {"partial": True}, "core_view": "自由生成的信息",
                "summary_evidence": {"core_view": view, "key_points": [view], "risks": [risk]}}
    rows = [{"report_id": i, "analysis_status": "done", "analysis_json": {**analysis, "material_type": material}}
            for i, material in enumerate(("commentary", "original"), 1)]
    monkeypatch.setattr(brief, "reports_ingested_between", lambda *_: rows)
    original, secondary = brief.load_morning_brief(date(2026, 11, 22))["reports"]
    assert original["partial"] and original["report_id"] == 2
    lead, caution = original["claims"]
    assert (lead["speaker"], lead["stance"], lead["location"]) == ("券商", "条件判断", "PDF 第3页")
    assert caution["quote"] == risk["source"]["quote"] and caution["speaker"] == "公司管理层"
    assert secondary["claims"][0]["speaker"] == "归属待核实"


def test_legacy_translation_must_still_match_the_current_source(monkeypatch):
    quote = "Management does not expect sales to rise."
    doc = Document([Segment(1, 4, quote)], "pdf_text")
    monkeypatch.setattr(brief, "load_document", lambda row: doc)

    def edition(statement, evidence):
        note = {"source": {"segment": 1, "page": 90, "quote": evidence}, "statement": statement,
                "stance": "expectation", "attribution": "broker"}
        row = {"report_id": 3, "analysis_status": "done", "analysis_json": json.dumps({
            "analysis_version": 2, "core_view": "旧版的自由判断", "evidence": [note]})}
        return brief._edition_report(row)

    checked = edition("管理层并不预计销售上涨。", quote)
    assert checked["status"] == "verified"
    assert checked["claims"][0]["speaker"] == "公司管理层" and checked["claims"][0]["location"] == "PDF 第4页"
    assert checked["claims"][0]["stance"] == "预期"
    excerpt = edition("预计销售上涨。", quote)
    assert excerpt["status"] == "excerpt" and excerpt["claims"][0]["quote"] == quote
    assert excerpt["claims"][0]["statement"] == ""
    assert edition("无来源的摘要", "unrelated historical passage")["status"] == "legacy"


def test_pending_old_and_malformed_items_remain_in_edition_count(monkeypatch):
    rows = [{"report_id": 1, "analysis_status": "none", "extraction_status": "pending_ocr"},
            {"report_id": 2, "analysis_status": "done", "analysis_json": {"core_view": "没有出处的旧观点"}},
            {"report_id": 3, "analysis_status": "failed", "analysis_json": "not-json"}]
    monkeypatch.setattr(brief, "reports_ingested_between", lambda *_: rows)
    issue = brief.load_morning_brief(date(2026, 11, 22))
    assert issue["total_count"] == 3
    assert [r["status"] for r in issue["reports"]] == ["pending", "legacy", "unavailable"]
    assert not any(r["claims"] for r in issue["reports"])


def test_original_analyst_voice_is_recovered_without_borrowing_embedded_interviews(monkeypatch):
    quote = "Our SOTP valuation remains unchanged and implies upside of 19%."
    note = {"kind": "valuation", "source": {"segment": 1, "quote": quote},
            "statement": "券商维持分部估值，对应19%上行空间。"}

    def report(body, material="original"):
        monkeypatch.setattr(brief, "load_document", lambda _: Document([Segment(1, 4, body)], "pdf_text"))
        return brief._edition_report({"report_id": 3, "analysis_status": "done", "analysis_json": {
            "material_type": material, "analysis_version": 3, "evidence": [note]}})

    assert report(quote)["claims"][0]["speaker"] == "券商"
    for r in (report('CFO said: “' + quote + '”'), report("Q&A transcript\n" + " " * 250 + quote),
              report(quote, "commentary"), report(quote + ', the CEO said.')):
        assert r["status"] == "excerpt" and r["claims"][0]["statement"] == ""


def test_current_rating_alone_cannot_be_presented_as_a_rating_action():
    quote = "We are Neutral as current valuations reflect the margin opportunities."
    for action in ("维持", "上调至", "下调至"):
        note = {"source": {"segment": 1, "page": 4, "quote": quote}, "attribution": "broker",
                "statement": f"{action}中性评级，估值已反映利润率机会。"}
        report = brief._edition_report({"report_id": 1, "analysis_status": "done", "analysis_json": {
            "analysis_version": 4, "material_type": "original", "evidence": [note]}})
        assert report["status"] == "excerpt"
        assert report["claims"][0]["quote"] == quote and not report["claims"][0]["statement"]
