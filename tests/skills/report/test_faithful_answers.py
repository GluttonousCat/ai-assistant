import json

from skills.base import SkillContext
from skills.report.skill import ReportSkill
from tools.finance import report_analysis


def run_qa(monkeypatch, claim, body):
    row = {"report_id": 71, "title": "经营计划跟踪", "org_name": "报告机构",
           "content_text": body, "analysis_json": '{"material_type":"original"}'}
    monkeypatch.setattr(report_analysis, "load_report", lambda rid: row)
    skill = ReportSkill()

    class LLM:
        def invoke(self, prompt):
            assert body in prompt
            return json.dumps({"claims": [claim]})

    monkeypatch.setattr(skill, "_llm", LLM)
    context = SkillContext(user_input="销量会增加吗？")
    skill._run_qa(context, {"report_id": 71})
    return context.result


def test_english_condition_keeps_speaker_and_full_original(monkeypatch):
    body = "Management expects shipments could increase if inventories normalize."
    claim = {"report_id": 71, "statement": "管理层预计，如果库存恢复正常，出货量可能增加。",
             "attribution": "management", "stance": "conditional", "source": {"segment": 1, "quote": body, "page": 45}}
    result = run_qa(monkeypatch, claim, body)
    checked, = result["citations"]
    assert checked["attribution"] == "management" and checked["stance"] == "conditional"
    assert checked["source"] == {"segment": 1, "page": None, "quote": body}
    assert body in result["summary"] and "库存恢复正常" in result["summary"]


def test_unfaithful_rendering_falls_back_to_quote_only(monkeypatch):
    body = "Management does not expect shipments to increase."
    claim = {"report_id": 71, "statement": "券商预计出货量会上升。", "attribution": "broker",
             "stance": "expectation", "source": {"segment": 1, "quote": body}}
    result = run_qa(monkeypatch, claim, body)
    assert body in result["summary"] and claim["statement"] not in result["summary"]
    checked, = result["citations"]
    assert checked["statement"] == "" and checked["attribution"] == "unknown"


def test_fabricated_quote_never_appears_in_answer(monkeypatch):
    body = "Management has not issued new guidance."
    claim = {"report_id": 71, "statement": "预计增长20%。", "source": {"segment": 1, "quote": "We expect 20% growth."}}
    result = run_qa(monkeypatch, claim, body)
    assert not result["citations"] and "20%" not in result["summary"]


def test_comparison_restores_speaker_context_and_skips_superseded_excerpts():
    from skills.report.answers import cited_passages
    from skills.report.document import Document, Segment

    fragment = "expects volumes to rise by 3%."
    sentence = "The company " + fragment
    document = Document([Segment(1, 1, sentence)], "pdf_text", 1)
    analysis = {"evidence": [
        {"statement": "销量预期", "source": {"segment": 1, "page": 1, "quote": fragment}},
        {"statement": "旧数据", "source": {"segment": 1, "page": 1, "quote": "expects sales to grow by 7%."}},
    ]}
    assert cited_passages(analysis, "销量", document) == [Segment(1, 1, sentence)]


def test_comparison_also_searches_pages_outside_the_saved_evidence():
    from skills.report.answers import comparison_passages
    from skills.report.document import Document, Segment

    quote = "管理层预计出货量持平。"
    expense = "质量保证费用计提方式于本年发生变更。"
    document = Document([Segment(1, 1, quote), Segment(2, 2, "背景。" * 2500), Segment(3, 3, expense)], "pdf_text", 3)
    analysis = {"evidence": [{"statement": quote, "source": {"segment": 1, "page": 1, "quote": quote}}]}
    passages = comparison_passages(analysis, "质量保证费用计提有什么变化？", document)
    assert any(p.page == 3 and expense in p.text for p in passages)
    assert sum(len(p.render()) + 2 for p in passages) <= 6000
