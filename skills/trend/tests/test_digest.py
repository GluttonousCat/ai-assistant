from datetime import date
import math

from skills.trend.digest import opportunity, render_html, select_stocks, export_csv, stock_notes


def row(code, score=65, *, eligible=False, fresh=True, name="示例股票"):
    return {"ts_code": code, "name": name, "score": score, "eligible": eligible,
            "trend60": {"fresh": fresh}, "state": "入场候选" if eligible else "早期反转观察",
            "context_pattern": "trend_pullback", "board": "主板", "metrics": {"close": 12}}


def test_threshold_and_candidate_scope_match_dashboard_without_promoting_observations():
    rows = [row("a", 75.1), row("b", 74.9), row("c", 90, fresh=False),
            row("d", 80, name="*ST示例"), row("e", math.nan),
            row("f", 76, eligible=True, fresh=False),
            row("g", 75, eligible=True), row("h", 75), row("i", 60), row("j", 59.9)]
    snapshot = {"metadata": {"config": {"fresh_reversal": True}}, "data": rows}
    selected = select_stocks(snapshot, minimum=75, exclusive=True)
    assert [r["ts_code"] for r in selected] == ["f", "a"]
    assert selected[1]["eligible"] is False
    assert [r["ts_code"] for r in select_stocks(snapshot, minimum=75, observations=False, exclusive=True)] == ["f"]
    assert [r["ts_code"] for r in select_stocks(snapshot)] == ["f", "g", "a", "h", "b", "i"]


def test_parent_context_has_the_same_presentation_priority_as_frontend():
    assert opportunity({"context_pattern": "trend_pullback", "pattern": "base_rebound"})[0] == "↑ 趋势回调"
    assert opportunity({"pattern": "unknown"})[0] == "· 待识别"
    assert opportunity({"context_pattern": "unknown", "pattern": "trend_pullback"})[0] == "↑ 趋势回调"


def test_html_groups_observations_and_entries_and_escapes_provider_text():
    rows = [row("a", eligible=True), row("b", name='<img src=x onerror="alert(1)">')]
    cards = [
        {"row": r, "charts": {period: {"cid": f"stock-{n}-{period}"} for period in ("1D", "60F")},
         "notes": stock_notes(r, {"industry_l2": "<行业>"})}
        for n, r in enumerate(rows)
    ]
    html = render_html({"as_of": "2026-11-13 15:00:00"}, cards, date(2026, 11, 15))
    assert "入场候选 · 1只" in html and "观察候选 · 1只" in html
    assert "&gt; 75" in html
    assert "休市日报" in html and "2026-11-13 15:00" in html
    assert '<img src=x onerror=' not in html and '&lt;img src=x' in html
    assert 'src="cid:stock-0-1D"' in html and 'src="cid:stock-0-60F"' in html
    assert "标的概况" in html and "走势解读" in html and "信号与风险" in html
    assert "&lt;行业&gt;" in html
    assert (html.index("信号与风险") < html.index('src="cid:stock-0-1D"')
            < html.index('src="cid:stock-0-60F"') < html.index('id="brief-2"')
            < html.rindex("信号与风险") < html.index('src="cid:stock-1-1D"')
            < html.index('src="cid:stock-1-60F"'))
    assert 'href="#chart-2-60f"' in html and 'name="chart-2-60f"' in html


def test_stock_notes_use_same_session_data_and_keep_missing_values_readable():
    candidate = {**row("600000.SH"), "reason": "日K转强阳线 / 新一轮入场", "stop_price": 11,
                 "risk_pct": 8.33, "parent_trend_label": "日K下跌背景"}
    data = {"industry_l2": "股份制银行", "total_mv": 12_500_000,
            "pe_ttm": None, "pb": .82, "pct_chg": -1.24}
    notes = stock_notes(candidate, data)
    assert "1,250.0 亿元" in notes["标的概况"] and "市净率 0.82 倍" in notes["标的概况"]
    assert "市盈率" not in notes["标的概况"]
    assert "收跌 1.24%" in notes["走势解读"] and "日K下跌背景" in notes["走势解读"]
    assert "日K转强阳线、新一轮入场" in notes["信号与风险"] and "参考止损 11.00 元" in notes["信号与风险"]


def test_empty_and_delayed_reports_never_insert_old_recommendations():
    empty = render_html({"as_of": "2026-11-13 15:00:00"}, [], date(2026, 11, 16))
    delayed = render_html(None, [], date(2026, 11, 16), delayed=True)
    assert "没有符合评分门槛" in empty
    assert "正式日报将在数据更新后发送" in delayed
    assert 'src="cid:' not in delayed


def test_csv_quotes_formula_like_provider_fields():
    output = export_csv([row("600000.SH", name="=cmd()")])
    assert "'=cmd()" in output
