from dataclasses import replace

import numpy as np
import pandas as pd

from skills.quant.market_context import MarketPolicy, index_context, attach_market
from skills.quant.prepare import MarketSeries
from skills.quant.tranche_prepare import confirm_signal_close
from skills.quant.signal_priority import attach_priority, prioritize
from skills.quant.report import with_benchmarks
import pytest
from skills.quant.tranche_engine import simulate
from tests.skills.quant.test_tranches import fixture, config
from tests.skills.trend.test_structure import candles


def with_market(stock):
    n = len(stock.times)
    for k in [
        "market_entry_allowed",
        "market_add_allowed",
        "market_context_valid",
        "market_systemic_weak",
        "market_resilient",
    ]:
        stock.data[k] = np.ones(n, dtype=bool)
    stock.data["market_beta"] = np.ones(n)
    stock.data["market_excess_pct"] = np.ones(n)
    return stock


def test_future_index_data_does_not_rewrite_past_regime():
    raw = candles(np.r_[100 + np.sin(np.arange(60) / 4), np.linspace(100, 110, 60)])
    frames = {"000300.SH": raw, "000852.SH": raw.copy()}
    full, *_ = index_context(frames)
    short, *_ = index_context({k: v.iloc[:80] for k, v in frames.items()})
    pd.testing.assert_frame_equal(short, full.loc[short.index])
    frames["000852.SH"] = raw.drop(raw.index[79])
    incomplete, *_ = index_context(frames)
    assert raw.bar_end.iloc[79] not in incomplete.index


def test_market_entry_decision_uses_signal_day_not_execution_day():
    s, t = fixture([10, 10.1, 10.2, 10.3])
    with_market(s)
    s.data["market_entry_allowed"][0] = False
    r = simulate([s], t, "swing_60f_v8", config(), MarketPolicy())
    assert (
        not r["open_positions"] and r["execution_audit"]["market_entry_rejected"] == 1
    )
    s.data["market_entry_allowed"][0] = True
    s.data["market_entry_allowed"][1] = False
    r = simulate([s], t, "swing_60f_v8", config(), MarketPolicy())
    assert len(r["open_positions"]) == 1


def test_systemic_panic_never_defers_a_price_stop():
    s, t = fixture([10, 9.7, 9.6, 9.5])
    with_market(s)
    r = simulate(
        [s],
        t,
        "swing_60f_v8",
        config(),
        MarketPolicy(entry_mode="none", soft_grace_days=1),
    )
    assert r["trades"][0]["reason"] == "stop_loss"
    assert not r["trades"][0]["market_grace_used"]


def test_soft_exit_receives_only_one_observation_session():
    s, t = fixture([10, 9.95, 9.94, 9.93, 9.92, 9.91])
    with_market(s)
    s.signals[0]["stop"] = 9.7
    s.data["tranche_trend_failed"][1:] = True
    cfg = config()
    cfg = replace(
        cfg, base=replace(cfg.base, trend_exit=True, min_trend_holding_bars=1)
    )
    r = simulate(
        [s],
        t,
        "swing_60f_v8",
        cfg,
        MarketPolicy(entry_mode="none", guard_adds=False, soft_grace_days=1),
    )
    assert r["execution_audit"]["systemic_soft_grace"] == 1
    assert pd.Timestamp(r["trades"][0]["exit_time"]).normalize() == t[3].normalize()
    assert r["trades"][0]["buy_legs"] == 1


def test_invalid_index_context_cannot_grant_a_soft_exit_delay():
    s, t = fixture([10, 9.95, 9.94, 9.93])
    with_market(s)
    s.signals[0]["stop"] = 9.7
    s.data["tranche_trend_failed"][1:] = True
    s.data["market_context_valid"][1:] = False
    cfg = config()
    cfg = replace(
        cfg, base=replace(cfg.base, trend_exit=True, min_trend_holding_bars=1)
    )
    r = simulate(
        [s], t, "swing_60f_v8", cfg, MarketPolicy(entry_mode="none", soft_grace_days=1)
    )
    assert pd.Timestamp(r["trades"][0]["exit_time"]).normalize() == t[2].normalize()


def test_relative_evidence_is_prefix_causal_and_rebase_invariant():
    values = 100 + np.sin(np.arange(240) / 7) + np.arange(240) / 30
    raw = candles(values)
    frames = {"000300.SH": raw, "000852.SH": candles(values * 1.01)}
    stock, _ = fixture(values[3::4])
    saved_scale = stock.data["scale"].copy()
    ctx, ret, ret4 = index_context(frames)
    full = attach_market(raw, stock, ctx, ret, ret4)
    n = 40
    short = MarketSeries(
        stock.code,
        stock.name,
        stock.observed_at,
        stock.times[:n],
        {k: v[:n] for k, v in stock.data.items()},
        [],
    )
    ctx_short, r_short, r4_short = index_context(
        {k: v.iloc[: n * 4] for k, v in frames.items()}
    )
    prefix = attach_market(raw.iloc[: n * 4], short, ctx_short, r_short, r4_short)
    rebased = replace(stock, data={**stock.data, "scale": saved_scale * 7})
    changed = attach_market(raw, rebased, ctx, ret, ret4)
    for key in full.data:
        if key.startswith("market_"):
            np.testing.assert_allclose(
                prefix.data[key], full.data[key][:n], equal_nan=True, atol=1e-10
            )
            np.testing.assert_allclose(
                changed.data[key], full.data[key], equal_nan=True, atol=1e-10
            )
    np.testing.assert_array_equal(stock.data["scale"], saved_scale)
    assert np.isnan(full.data["market_beta"][:10]).all()


def test_close_retention_uses_native_trigger_timestamp_and_adjusted_units():
    raw = candles([10, 10.2, 10.3, 10.1, 10, 10.1, 10.2, 10.4])
    stock, _ = fixture([10.1, 10.4])
    stock.signals = [
        {**stock.signals[0], "leg": raw.bar_end.iloc[1].isoformat()},
        {**stock.signals[0], "i": 1, "leg": raw.bar_end.iloc[5].isoformat()},
    ]
    result = confirm_signal_close(raw, stock)
    assert [s["close_held"] for s in result.signals] == [False, True]
    changed = confirm_signal_close(
        raw, replace(stock, data={**stock.data, "scale": np.full(2, 3.0)})
    )
    assert [s["close_held"] for s in changed.signals] == [False, True]
    assert all("close_held" not in s for s in stock.signals)


def test_signal_priority_is_causal_and_does_not_mutate_original_scores():
    values = 100 + np.sin(np.arange(240) / 8) + np.arange(240) / 30
    raw = candles(values)
    stock, _ = fixture(values[3::4])
    with_market(stock)
    for i in range(20, len(stock.times)):
        stock.signals.append(
            {**stock.signals[0], "i": i, "stop": values[4 * i + 3] - 1}
        )
    returns = raw.set_index("bar_end").close.pct_change(20, fill_method=None)
    full = attach_priority(raw, stock, returns)
    n = 40
    short = replace(
        stock,
        times=stock.times[:n],
        data={k: v[:n] for k, v in stock.data.items()},
        signals=[s for s in stock.signals if s["i"] < n],
    )
    prefix = attach_priority(raw.iloc[: n * 4], short, returns.iloc[: n * 4])
    for key in ["priority_relative", "priority_extension", "priority_slope"]:
        np.testing.assert_allclose(prefix.data[key], full.data[key][:n], equal_nan=True)
    for style in ("support", "relative", "blended"):
        a = prioritize(full, style, 2.5)
        b = prioritize(prefix, style, 2.5)
        assert [s for s in a.signals if s["i"] < n] == b.signals
    assert all(s["score"] == 90 and "original_score" not in s for s in stock.signals)


def test_benchmarks_align_by_date_without_mutating_portfolio_results():
    result = {
        "equity": [{"date": "b", "equity": 100}, {"date": "c", "equity": 101}],
        "metrics": {"total_return_pct": 1},
    }
    reference = [
        {"date": d, "benchmarks": {"000300.SH": p, "000852.SH": p * 2}}
        for d, p in [("c", 1.8), ("a", 1), ("b", 1.5)]
    ]
    enriched = with_benchmarks(result, reference)
    assert enriched["equity"][0]["benchmarks"]["000300.SH"] == 1
    assert enriched["equity"][1]["benchmarks"]["000852.SH"] == pytest.approx(1.2)
    assert enriched["metrics"] == result["metrics"]
    assert all("benchmarks" not in p for p in result["equity"])
    with pytest.raises(ValueError, match="基准"):
        with_benchmarks(result, reference[:2])
