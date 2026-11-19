from decimal import Decimal

from harness.mcp.tools import reports
from skills.beta_alpha.analysis.alpha_context import report_views
from skills.content.profile import profile_digest


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
