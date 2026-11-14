from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from skills.trend.swing import indicators
from skills.trend.structure import StructureConfig, ordered_evidence, setups


def candles(close, volume=None):
    close = np.asarray(close, dtype=float)
    times = [
        day + pd.Timedelta(hours=h, minutes=m)
        for day in pd.bdate_range("2026-01-05", periods=100)
        for h, m in [(10, 30), (11, 30), (14, 0), (15, 0)]
    ]
    return pd.DataFrame(
        dict(
            bar_end=times[: len(close)],
            open=close - 0.3,
            high=close + 0.4,
            low=close - 0.4,
            close=close,
            volume=np.asarray(volume)
            if volume is not None
            else np.full(len(close), 1000.0),
            amount=np.full(len(close), 1e8),
        )
    )


def pullback_fixture():
    warm = 100 + np.sin(np.arange(40)) * 0.1
    close = np.r_[
        warm,
        99.0,
        np.linspace(100, 112, 25),
        np.linspace(111.5, 108.8, 5),
        np.linspace(109.5, 114, 7),
        np.linspace(115, 118, 5),
        np.linspace(117.5, 114.9, 5),
        np.linspace(115.5, 120, 7),
    ]
    volume = np.full(len(close), 1000.0)
    volume[66:71] = 450
    volume[83:88] = 450
    volume[71:78] = 1400
    volume[88:] = 1400
    return candles(close, volume)


def test_ordered_advance_and_contraction_produce_a_resumption():
    f = indicators(pullback_fixture())
    e = ordered_evidence(f)
    resumed = e[e.ordered_trigger]
    assert not resumed.empty
    assert {1, 2}.issubset(set(resumed.pullback_round))
    for i, row in resumed.iterrows():
        assert row.impulse_start < row.impulse_peak < f.bar_end.iloc[i]
        assert row.ordered_floor < f.close.iloc[i]


def test_decline_then_bounce_is_not_an_uptrend_pullback():
    close = np.r_[np.full(40, 110.0), np.linspace(110, 90, 20), np.linspace(90, 94, 8)]
    e = ordered_evidence(indicators(candles(close)))
    assert not e.ordered_trigger.any()


def test_high_volume_retracement_is_not_a_contracted_test():
    raw = pullback_fixture()
    raw.loc[66:70, "volume"] = 5000.0
    e = ordered_evidence(indicators(raw))
    assert not e.ordered_trigger.iloc[66:78].any()


def test_future_prices_and_volume_cannot_change_old_structure():
    raw = pullback_fixture()
    full = setups(indicators(raw))
    for cut in [43, 65, 71, 77, 90]:
        prefix = setups(indicators(raw.iloc[:cut]))
        pd.testing.assert_frame_equal(prefix, full.iloc[:cut].reset_index(drop=True))


def test_confirmed_support_is_never_available_at_its_low_bar():
    f = indicators(pullback_fixture())
    e = ordered_evidence(f)
    for i, row in e[e.support_origin.notna()].iterrows():
        origin = f.index[f.bar_end.eq(row.support_origin)][0]
        assert i >= origin + 2


def test_structure_thresholds_reject_invalid_ratios():
    with pytest.raises(ValueError):
        replace(StructureConfig(), max_retrace_fraction=1.2)


def test_tested_bottom_requires_a_bounce_and_contracting_second_low():
    close = np.r_[
        100 + np.sin(np.arange(80)) * 0.1,
        np.linspace(100, 90, 12),
        np.linspace(90.8, 95, 8),
        np.linspace(94.8, 90.2, 10),
        np.linspace(91, 98, 10),
    ]
    volume = np.full(len(close), 1000.0)
    volume[89:94] = 1600.0
    volume[106:112] = 450.0
    volume[112:] = 1500.0
    raw = candles(close, volume)
    f = setups(indicators(raw))
    assert f.b_context.iloc[111:].any()
    assert not f.b_context.iloc[100:111].any()
    # Same prices and MACD, but a high-volume second low is not a supply test.
    raw.loc[106:111, "volume"] = 5000.0
    assert not setups(indicators(raw)).b_context.iloc[111:].any()


def test_economic_structure_is_invariant_to_factor_rebasing():
    raw = pullback_fixture()
    a = setups(indicators(raw))
    raw[["open", "high", "low", "close"]] *= 0.137
    b = setups(indicators(raw))
    for key in ["ordered_trigger", "eligible", "b_context", "pullback_round"]:
        assert a[key].tolist() == b[key].tolist()
