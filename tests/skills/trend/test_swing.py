from dataclasses import replace

import numpy as np
import pandas as pd

from skills.trend.swing import (
    SwingConfig,
    indicators,
    divergence_evidence,
    setups,
    execution_plans,
)


def bars():
    close = np.r_[
        np.full(20, 100.0),
        np.linspace(100, 70, 21)[1:],
        np.linspace(70, 83, 9)[1:],
        np.linspace(83, 69.5, 21)[1:],
        np.linspace(69.5, 85, 13)[1:],
    ]
    times = [
        day + pd.Timedelta(hours=h, minutes=m)
        for day in pd.bdate_range("2026-01-05", periods=30)
        for h, m in [(10, 30), (11, 30), (14, 0), (15, 0)]
    ]
    return pd.DataFrame(
        dict(
            bar_end=times[: len(close)],
            open=close - 0.3,
            high=close + 0.4,
            low=close - 0.4,
            close=close,
            volume=np.full(len(close), 1e6),
            amount=np.full(len(close), 1e8),
        )
    )


def test_divergence_only_becomes_visible_after_pivot_confirmation():
    f = indicators(bars())
    evidence = divergence_evidence(f, 0.03)
    active = evidence[evidence.divergence]
    assert not active.empty
    for i, row in active.iterrows():
        pivot = f.index[f.bar_end.eq(row.divergence_origin)][0]
        confirmed = f.index[f.bar_end.eq(row.divergence_confirmed)][0]
        assert confirmed == pivot + 2 and i >= confirmed
    assert not divergence_evidence(f.iloc[:confirmed], 0.03).divergence.any()


def test_future_bars_never_change_past_divergence_or_entry_signals():
    raw = bars()
    full = setups(indicators(raw))
    for cut in [45, 68, 70, 75]:
        prefix = setups(indicators(raw.iloc[:cut]))
        pd.testing.assert_frame_equal(prefix, full.iloc[:cut].reset_index(drop=True))


def plan_inputs():
    f = indicators(bars()).iloc[:80].copy()
    f["eligible"] = False
    f["pattern"] = "base_rebound"
    f["stop_price"] = f.close * 0.98
    f["score"] = 90.0
    f.loc[f.index[-1], "eligible"] = True
    # Explicitly valid close context, independently of a daily candle shape.
    f.loc[f.index[-1], ["ma8", "ma13", "atr"]] = [
        f.close.iloc[-1] - 0.1,
        f.close.iloc[-1] - 0.1,
        1.0,
    ]
    daily = pd.DataFrame(dict(bar_end=f.bar_end.iloc[3::4].to_numpy(), amount=1e8))
    return f, daily


def test_daily_candle_shape_is_not_an_entry_gate():
    f, daily = plan_inputs()
    a = execution_plans(f, daily)
    assert len(a) == 1
    daily = daily.assign(open=100.0, high=101.0, low=80.0, close=81.0)
    b = execution_plans(f, daily)
    pd.testing.assert_frame_equal(a, b)


def test_missing_native_slot_or_failed_close_cancels_plan():
    f, daily = plan_inputs()
    assert execution_plans(f.drop(f.index[-2]), daily).empty
    f.loc[f.index[-1], "ma8"] = f.close.iloc[-1] + 1
    assert execution_plans(f, daily).empty


def test_risk_limit_cannot_be_relaxed_above_five_percent():
    import pytest

    with pytest.raises(ValueError):
        replace(SwingConfig(), max_risk_pct=6)
