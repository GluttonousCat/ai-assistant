from datetime import date
import math

from skills.trend.digest import opportunity, render_html, select_stocks, export_csv


def row(code, score=65, *, eligible=False, fresh=True, name="示例股票"):
    return {"ts_code": code, "name": name, "score": score, "eligible": eligible,
            "trend60": {"fresh": fresh}, "state": "入场候选" if eligible else "早期反转观察",
            "context_pattern": "trend_pullback", "board": "主板", "metrics": {"close": 12}}


def test_threshold_and_candidate_scope_match_dashboard_without_promoting_observations():
    rows = [row("a", 60), row("b", 59.9), row("c", 90, fresh=False),
            row("d", 70, name="*ST示例"), row("e", math.nan),
            row("f", 60, eligible=True, fresh=False)]
    snapshot = {"metadata": {"config": {"fresh_reversal": True}}, "data": rows}
    selected = select_stocks(snapshot)
    assert [r["ts_code"] for r in selected] == ["f", "a"]
    assert selected[1]["eligible"] is False
    assert [r["ts_code"] for r in select_stocks(snapshot, observations=False)] == ["f"]


def test_parent_context_has_the_same_presentation_priority_as_frontend():
    assert opportunity({"context_pattern": "trend_pullback", "pattern": "base_rebound"})[0] == "↑ 趋势回调"
    assert opportunity({"pattern": "unknown"})[0] == "· 待识别"
    assert opportunity({"context_pattern": "unknown", "pattern": "trend_pullback"})[0] == "↑ 趋势回调"


def test_html_groups_observations_and_entries_and_escapes_provider_text():
    rows = [row("a", eligible=True), row("b", name='<img src=x onerror="alert(1)">')]
    cards = [{"row": r, "cid": f"stock-{n}", "png": b"png"} for n, r in enumerate(rows)]
    html = render_html({"as_of": "2026-11-13 15:00:00"}, cards, date(2026, 11, 15))
    assert "入场候选 · 1只" in html and "观察候选 · 1只" in html
    assert "休市日报" in html and "2026-11-13 15:00" in html
    assert '<img src=x onerror=' not in html and '&lt;img src=x' in html
    assert 'src="cid:stock-0"' in html


def test_empty_and_delayed_reports_never_insert_old_recommendations():
    empty = render_html({"as_of": "2026-11-13 15:00:00"}, [], date(2026, 11, 16))
    delayed = render_html(None, [], date(2026, 11, 16), delayed=True)
    assert "没有符合评分门槛" in empty
    assert "不会将旧名单冒充今日结果" in delayed
    assert 'src="cid:' not in delayed


def test_csv_quotes_formula_like_provider_fields():
    output = export_csv([row("600000.SH", name="=cmd()")])
    assert "'=cmd()" in output
