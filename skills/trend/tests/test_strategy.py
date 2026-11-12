from dataclasses import replace
import numpy as np
import pandas as pd
import pytest

from skills.trend.strategy import TrendConfig, indicators, screen
from tools.market.trend_data import adjust_prices


def sample(days=90, direction=1):
    dates = pd.bdate_range("2026-01-05", periods=days)
    times = [
        day + pd.Timedelta(hours=h, minutes=m)
        for day in dates
        for h, m in [(10, 30), (11, 30), (14, 0), (15, 0)]
    ]
    close = 20 + direction * np.arange(len(times)) * 0.015
    return pd.DataFrame(
        {
            "bar_end": times,
            "open": close - 0.015,
            "high": close + 0.09,
            "low": close - 0.09,
            "close": close,
            "volume": 1_000_000.0,
            "amount": 20_000_000.0,
            "adj_factor": 1.0,
        }
    )


def test_smooth_rising_structure_becomes_pullback_candidate():
    result = screen(sample())
    assert result["eligible"]
    assert result["state"] == "回踩候选"
    assert all(c["passed"] for c in result["checks"])
    assert (
        result["stop_price"]
        <= result["metrics"]["close"] - 2 * result["metrics"]["atr"] + 0.0001
    )


def test_breakout_excludes_current_bar_from_resistance_and_requires_volume():
    f = sample()
    f.loc[f.index[-1], ["close", "high"]] += 0.25
    f.loc[f.index[-1], "volume"] *= 2
    out = indicators(f)
    assert out.iloc[-1].prior_high == f.high.iloc[-41:-1].max()
    assert out.iloc[-1].breakout
    assert screen(f)["state"] == "突破候选"
    f.loc[f.index[-1], "volume"] = 0
    assert not indicators(f).iloc[-1].breakout


def test_calculation_is_prefix_invariant_even_for_daily_confirmation():
    f = sample()
    full = indicators(f)
    for length in [285, 286, 287, 288, 319, 320]:
        prefix = indicators(f.iloc[:length])
        columns = [
            "ema_fast",
            "ema_slow",
            "atr",
            "adx",
            "efficiency",
            "prior_high",
            "volume_ratio",
            "daily_fast",
            "daily_slow",
            "daily_amount",
            "daily_trend",
            "breakout",
            "pullback",
        ]
        pd.testing.assert_series_equal(
            prefix.iloc[-1][columns], full.iloc[length - 1][columns], check_names=False
        )


def test_flat_and_falling_prices_never_pass():
    assert not screen(sample(direction=-1))["eligible"]
    flat = sample(direction=0)
    assert not screen(flat)["eligible"]
    assert indicators(flat).efficiency.eq(0).all()
    assert not screen(sample(days=20))["eligible"]


def test_loose_risk_does_not_turn_into_tight_stop_to_force_a_candidate():
    f = sample()
    f.loc[f.index[-5], "low"] = 10
    result = screen(f)
    assert not result["eligible"]
    assert result["stop_price"] < 10
    assert not next(c for c in result["checks"] if c["key"] == "risk")["passed"]


def test_gate_cannot_be_overridden_by_high_score():
    result = screen(sample(), replace(TrendConfig(), min_daily_amount=1e12))
    assert result["score"] > 70
    assert not result["eligible"]
    assert "流动性" in result["reason"]


def test_ex_rights_gap_is_removed_using_date_matched_factors():
    f = sample()
    midpoint = len(f) // 2
    f.loc[f.index[midpoint:], "adj_factor"] = 2
    f.loc[f.index[midpoint:], ["open", "high", "low", "close"]] /= 2
    adjusted, issue = adjust_prices(f)
    assert issue is None
    np.testing.assert_allclose(adjusted.close, sample().close / 2)
    assert adjusted.raw_close.iloc[-1] == f.close.iloc[-1]
    assert adjusted.amount.equals(f.amount)


def test_missing_factor_is_never_filled_with_past_or_future_value():
    f = sample()
    f.loc[f.index[-1], "adj_factor"] = np.nan
    _, issue = adjust_prices(f)
    assert issue == "复权因子缺失"
    f = sample()
    f.loc[f.index[-1], "amount"] = np.inf
    assert adjust_prices(f)[1] == "价格或成交数据无效"


def test_config_rejects_invalid_parameters():
    for args in [
        {"fast": 60, "slow": 20},
        {"min_efficiency": 0},
        {"max_risk_pct": float("nan")},
        {"stop_atr": -1},
    ]:
        with pytest.raises(ValueError):
            TrendConfig(**args)
