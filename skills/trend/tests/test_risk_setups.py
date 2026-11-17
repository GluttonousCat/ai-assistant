from pathlib import Path
import numpy as np
import pandas as pd

from skills.trend.mtf import MtfConfig, evaluate, features, result
from skills.trend.pullback import first_pullback
from skills.trend.pullback import controlled_pullbacks
from skills.trend.tests.test_reversal import bars


CFG = MtfConfig(fresh_reversal=True, reversal_cycle_reset=True, risk_setups=True)


def case():
    f = pd.read_csv(
        Path(__file__).parent / "fixtures/sugon_20261016.csv", parse_dates=["bar_end"]
    )
    return tuple(f[f.timeframe.eq(t)].reset_index(drop=True) for t in ["60F", "1D"])


def test_sugon_gap_held_is_not_filtered_as_a_small_daily_body():
    m, d = case()
    _, out = evaluate(m, d, CFG)
    end = pd.Timestamp("2026-10-16 15:00")
    r = out[out.bar_end.eq(end)].iloc[0]
    assert r.gap_held and r.eligible and r.pattern_label == "低位缺口守稳"
    assert 0 < r.risk_pct <= 5 and r.stop_price < r.low
    assert all(c["passed"] for c in result(out.loc[: r.name], CFG)["checks"])
    _, old = evaluate(m, d, MtfConfig(fresh_reversal=True, reversal_cycle_reset=True))
    assert not old[old.bar_end.eq(end)].iloc[0].eligible


def test_gap_cannot_borrow_future_missing_60f_or_fill_its_gap():
    m, d = case()
    end = pd.Timestamp("2026-10-16 15:00")
    d = d[d.bar_end <= end].copy()
    for minute in [m[m.bar_end < end], m[~m.bar_end.eq("2026-10-16 11:30")]]:
        _, out = evaluate(minute, d, CFG)
        assert not out.iloc[-1].eligible
    d.loc[d.bar_end.eq(end), "low"] = 66.5
    _, out = evaluate(m, d, CFG)
    assert not out.iloc[-1].gap_held and not out.iloc[-1].eligible


def test_risk_setups_are_prefix_and_scale_invariant():
    m, d = case()
    _, full = evaluate(m, d, CFG)
    end = pd.Timestamp("2026-10-16 15:00")
    _, cut = evaluate(m[m.bar_end <= end], d[d.bar_end <= end], CFG)
    pd.testing.assert_series_equal(
        cut.eligible, full.loc[full.bar_end <= end, "eligible"]
    )
    for f in [m, d]:
        f[["open", "high", "low", "close"]] *= 1.23456789
    _, scaled = evaluate(m, d, CFG)
    pd.testing.assert_series_equal(full.eligible, scaled.eligible)


def test_first_pullback_does_not_become_repeated_first_entries():
    path = (
        [10] * 60
        + list(np.linspace(10.1, 13, 30))
        + [12.9, 12.8, 12.7, 13.1, 13.2, 13.0, 12.9, 13.3]
    )
    f = features(bars(path))
    r = first_pullback(f)
    assert r.any()
    assert not r.iloc[-3:].any()
    pd.testing.assert_series_equal(first_pullback(f.iloc[:-3]), r.iloc[:-3])


def test_later_controlled_pullback_remains_an_opportunity_with_its_own_round():
    path = (
        [10] * 60
        + list(np.linspace(10.1, 13, 30))
        + [12.9, 12.8, 12.7, 13.1, 13.2, 13.0, 12.9, 13.3, 13.2, 13.1, 13.45]
    )
    context = controlled_pullbacks(features(bars(path)))
    assert (context.active & context["rounds"].eq(2)).any()
    assert not context.loc[context["rounds"].gt(1), "first"].any()


def test_historical_scan_can_reuse_verified_cutoffs_but_not_skip_bar_checks(
    monkeypatch,
):
    import pytest
    from jobs import scan_trend

    monkeypatch.setattr(
        scan_trend,
        "load_dashboard",
        lambda *a: dict(
            run_id="prior",
            as_of="2026-11-12 15:00",
            metadata=dict(data_latest="2026-11-12 15:00", factor_latest="2026-11-12"),
        ),
    )
    monkeypatch.setattr(
        scan_trend,
        "scan_cutoffs",
        lambda *a: pytest.fail("Should not rescan every historical table"),
    )
    monkeypatch.setattr(scan_trend, "scan_universe", lambda *a, **kw: [])
    with pytest.raises(ValueError, match="成分股快照"):
        scan_trend.scan("2026-11-12", CFG)


def test_new_rules_notice_does_not_reinterpret_old_snapshots(monkeypatch):
    from skills.trend import service
    from skills.trend.mtf import DUAL_VERSION

    old = dict(
        version="60f-early-reversal-daily-entry-v6.1", data=[dict(eligible=False)]
    )
    monkeypatch.setattr(service, "load_dashboard", lambda *a: old)
    monkeypatch.setattr(service, "refresh_freshness", lambda snapshot: {})
    latest = service.dashboard()
    assert latest["latest_rules_version"] == DUAL_VERSION
    assert latest["version"] == old["version"] and latest["data"] == old["data"]
    assert "latest_rules_version" not in old
    assert "latest_rules_version" not in service.dashboard("historical-id")
