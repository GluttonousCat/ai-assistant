import json

import pytest

from skills.report.document import Document, Segment
from skills.report.extraction import analyze_document, locate_excerpt, verify_claim


def claim(statement, quote, **fields):
    return {"kind": "driver", "statement": statement, "attribution": "broker", "stance": "expectation",
            "source": {"segment": 1, "quote": quote}, **fields}


@pytest.mark.parametrize(("quote", "statement"), [
    ("Management does not expect margins to rise.", "管理层预计利润率上升。"),
    ("Margins may rise if demand recovers.", "利润率会上升。"),
    ("Margins may rise by 5%.", "预计利润率增长5%。"),
    ("We expect margins to rise.", "利润率上升。"),
    ("We expect margins to rise.", "我们不认为利润率将上升。"),
    ("We expect margins to rise.", "仅在销量回升的情况下，预计利润率上升。"),
    ("We expect margins to rise.", "We expect margins to rise."),
    ("Respondents expect growth to fall from 55% to 5%.", "受访者预计增速从5%降至55%。"),
    ("Net confidence dropped by 5 percentage points.", "净信心下降5%。"),
    ("We estimate sales of USD 5 million.", "营收预计为5百万港元。"),
    ("We estimate sales of USD 5 million.", "营收预计为5亿美元。"),
    ("We estimate sales of USD 5 million.", "营收预计为5美元。"),
    ("We expect profits to fall next year.", "明年利润预计增长。"),
])
def test_known_translation_distortions_are_excluded(quote, statement):
    assert verify_claim(claim(statement, quote), [Segment(1, 3, quote)], material="original") is None


@pytest.mark.parametrize(("quote", "statement", "speaker"), [
    ("Management expects sales to rise.", "管理层预计销售额上升。", "management"),
    ("Management is more confident on diesel engines.", "管理层对柴油发动机更有信心。", "management"),
    ("Survey respondents expect sales to fall.", "受访者预计销售额下降。", "survey"),
    ("Unlike consensus estimates, we expect growth.", "与一致预期不同，研究员预计增长。", "broker"),
    ("We disagree with management expectations and expect growth.", "研究员不同意管理层的预期，预计增长。", "broker"),
])
def test_attribution_of_reported_speech(quote, statement, speaker):
    entry = verify_claim(claim(statement, quote), [Segment(1, 3, quote)], material="original")
    assert entry["attribution"] == speaker


def test_quote_cannot_omit_same_sentence_qualification():
    original = "如果销量回暖，则利润率可能回升。"
    item = claim("利润率一定回升。", "则利润率可能回升。")
    segments = [Segment(1, 2, original)]
    verified = verify_claim(item, segments, material="original")
    assert verified["statement"] == verified["source"]["quote"]
    assert "如果销量回暖" in verified["statement"]
    assert locate_excerpt(item, segments) == verified["source"]


def test_original_summary_only_uses_verified_claims_and_initial_rating():
    issuer = "Example Securities Research"
    management = "Management expects orders to rise."
    view = "We expect sales to grow if demand improves."
    risk = "Export restrictions could reduce sales."
    body = "\n".join([issuer, management, view, risk, "We maintain a Hold rating."])
    notes = [
        claim("管理层预计订单上升。", management, insight_type="theses"),
        claim("若需求改善，预计销售额增长。", view, insight_type="theses"),
        claim("出口限制可能降低销售额。", risk, kind="risk", stance="risk"),
        claim("一定会大涨。", "no matching quotation"),
    ]
    data = {"material": {"type": "original", "source": {"segment": 1, "quote": issuer}},
            "evidence": notes, "core_view": "全部买入", "key_points": ["没有出处"],
            "risks": ["没有风险"], "rating": "买入", "rating_source": {"segment": 1, "quote": "We maintain a Hold rating."}}

    class LLM:
        def stream(self, _):
            yield json.dumps(data)

    events = list(analyze_document(Document([Segment(1, 1, body)], "pdf_text", 1), "样本", LLM(), streaming=True))
    assert all(e["type"] != "delta" for e in events)
    result = events[-1]["data"]
    summary = result["summary_evidence"]
    assert summary["core_view"]["source"]["quote"] == view
    assert [note["source"]["quote"] for note in summary["key_points"]] == [management]
    assert [note["source"]["quote"] for note in summary["risks"]] == [risk]
    assert result["core_view"].endswith(notes[1]["statement"])
    assert result["rating"] == "无" and result["rating_source"] is None
