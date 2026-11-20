import json

import pytest

from skills.report.document import Document, Segment
from skills.report.extraction import analyze_document


@pytest.mark.parametrize(("material", "line", "quote", "proposed", "expected"), [
    ("original", "Stock Rating  Overweight", "Stock Rating  Overweight", "增持", "增持"),
    ("original", "我们维持公司“买入”评级。", "我们维持公司“买入”评级。", "买入", "买入"),
    ("original", "We maintain a Hold rating.", "We maintain a Hold rating.", "持有", "持有"),
    ("original", "Customers buy diesel engines.", "buy diesel engines", "买入", "无"),
    ("original", "We do not rate this stock a Buy.", "We do not rate this stock a Buy.", "买入", "无"),
    ("original", "Previous Stock Rating: Buy", "Stock Rating: Buy", "买入", "无"),
    ("original", "Rating definitions\nRating: Buy means 15% upside", "Rating: Buy", "买入", "无"),
    ("original", "Rating: Buy means 15% upside", "Rating: Buy", "买入", "无"),
    ("commentary", "Stock Rating  Overweight", "Stock Rating  Overweight", "增持", "无"),
    ("unknown", "Stock Rating  Overweight", "Stock Rating  Overweight", "增持", "无"),
])
def test_current_rating_requires_direct_current_rating_in_original(material, line, quote, proposed, expected):
    issuer = "Example Securities Research"
    document = Document([Segment(1, 1, issuer + "\n" + line)], "pdf_text", 1)

    class LLM:
        def invoke(self, _):
            return json.dumps({"market": "A股", "material": {"type": material, "source": {"segment": 1, "quote": issuer}},
                               "rating": proposed, "rating_source": {"segment": 1, "quote": quote}})

    result = list(analyze_document(document, "评级样本", LLM()))[-1]["data"]
    assert result["rating"] == expected
    assert result["rating_source"] == ({"segment": 1, "page": 1, "quote": quote} if expected != "无" else None)
