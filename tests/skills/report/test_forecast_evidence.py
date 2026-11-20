import json

from skills.report.document import Document, Segment
from skills.report.extraction import analyze_document


def test_forecast_column_period_metric_and_scale_are_bound_to_original_table():
    header = "Fiscal Year Ending  12/25  12/26e  12/27e  12/28e"
    row = "Revenue, net (Rmb mn) 231,809 245,993 283,282 317,662"
    body = header + "\n2026 sales were discussed but 2027 contracts are pending.\n" + row
    forecast = {"metric": "revenue", "period": "2027E", "unit": "百万元人民币",
                "value": 283282, "source": {"segment": 1, "quote": row}}
    proposals = [forecast, {**forecast, "value": 245993}, {**forecast, "period": "2025E", "value": 231809},
                 {**forecast, "unit": "亿元人民币"}, {**forecast, "metric": "eps"}]

    class LLM:
        def invoke(self, _):
            return json.dumps({"forecasts": proposals})

    data = list(analyze_document(Document([Segment(1, 3, body)], "pdf_text", 3), "表格", LLM()))[-1]["data"]
    accepted, = data["forecasts"]
    assert accepted["value"] == 283282
    assert accepted["header_source"] == {"segment": 1, "page": 3, "quote": header}
    assert data["forecast_excerpts"] == [{"page": 3, "segment": 1, "quote": row}]
    assert data["coverage"]["rejected_forecasts"] == 4


def test_absent_header_keeps_only_excerpt_and_single_price_target_is_verifiable():
    table = "营业收入（亿元） 30 25 20"
    price = "Price target  Rmb32.00"
    body = table + "\n" + price

    class LLM:
        def invoke(self, _):
            return json.dumps({"forecasts": [
                {"metric": "revenue", "period": "2026E", "value": 25, "unit": "亿元",
                 "source": {"segment": 1, "quote": table}},
                {"metric": "target_price", "period": "NA", "value": 32, "unit": "元",
                 "header_source": {"quote": "invented"}, "source": {"segment": 1, "quote": price}},
            ]})

    data = list(analyze_document(Document([Segment(1, 1, body)], "pdf_text", 1), "表格", LLM()))[-1]["data"]
    assert [f["metric"] for f in data["forecasts"]] == ["target_price"]
    assert "header_source" not in data["forecasts"][0]
    assert data["forecast_excerpts"][0]["quote"] == table
