import numpy as np
import pandas as pd

from skills.trend.wyckoff import events, features
from skills.trend.tests.test_structure import candles


def evidence(rows):
    times = candles(np.full(len(rows), 10)).bar_end
    f = pd.DataFrame(rows, columns=["open", "high", "low", "close", "effort"])
    f["bar_end"] = times
    f["volume"] = 1000
    f["atr"] = 1.0
    f["spread"] = f.high - f.low
    f["close_location"] = (f.close - f.low) / f.spread
    f["floor20"], f["ceiling20"], f["ceiling8"] = 10.0, 15.0, 12.0
    return f


def spring_frame():
    return evidence(
        [
            [9.9, 10.5, 9.6, 10.3, 2.0],
            [10.3, 10.6, 10.0, 10.4, 1.2],
            [10.1, 10.2, 9.8, 10.1, 0.5],
            [10.1, 10.25, 9.95, 10.15, 0.6],
            [10.15, 10.7, 10.1, 10.6, 1.3],
        ]
    )


def test_spring_requires_low_effort_test_and_later_demand_confirmation():
    f = spring_frame()
    result = events(f)
    assert list(result.event) == ["", "", "", "", "spring_test"]
    assert result.support.iloc[-1] == 9.8
    assert result.support_origin.iloc[-1] == f.bar_end.iloc[2]
    assert result.support_known.iloc[-1] == f.bar_end.iloc[4]
    for n in [2, 3, 4]:
        prefix = events(f.iloc[:n])
        pd.testing.assert_frame_equal(prefix, result.iloc[:n])
    f.loc[2:3, "effort"] = 2.0
    assert not events(f).event.ne("").any()


def test_lps_requires_sos_then_contracting_supply_and_resumed_demand():
    f = evidence(
        [
            [11.5, 13.2, 11.4, 13.0, 2.0],
            [13.0, 14.0, 12.9, 13.8, 1.8],
            [13.8, 13.8, 13.0, 13.2, 0.8],
            [13.2, 13.3, 12.9, 13.0, 0.6],
            [13.0, 13.25, 12.95, 13.1, 0.6],
            [13.1, 13.8, 13.05, 13.7, 1.2],
        ]
    )
    result = events(f)
    assert result.event.iloc[-1] == "lps"
    assert result.contraction.iloc[-1] < 0.8
    assert result.support.iloc[-1] == 12.9
    f.loc[2:3, "effort"] = 3.0
    assert not events(f).event.eq("lps").any()


def test_spring_test_confirmation_restarts_when_a_right_bar_makes_a_lower_low():
    f = spring_frame()
    f.loc[3, "low"] = 9.7
    last = evidence([[10.6, 10.9, 10.4, 10.8, 1.2]])
    last["bar_end"] = f.bar_end.iloc[-1] + pd.Timedelta(hours=1)
    f = pd.concat([f, last], ignore_index=True)
    f["spread"] = f.high - f.low
    f["close_location"] = (f.close - f.low) / f.spread
    result = events(f)
    assert result.event.iloc[4] == ""
    assert result.event.iloc[5] == "spring_test"
    assert result.support.iloc[-1] == 9.7
    assert result.support_origin.iloc[-1] == f.bar_end.iloc[3]


def test_high_volume_at_a_low_is_not_automatically_distribution():
    high = evidence([[15.0, 15.2, 14.5, 14.6, 2.2], [14.6, 14.7, 14.1, 14.2, 1.3]])
    result = events(high)
    assert result.supply_warning.iloc[0] and not result.supply_defense.iloc[0]
    assert result.supply_defense.iloc[1]
    low = evidence([[10.9, 11.1, 10.5, 10.6, 2.2]])
    assert not events(low).supply_warning.iloc[0]


def test_relative_volume_compares_same_clock_and_features_are_prefix_causal():
    f = candles(np.full(80, 10))
    f["volume"] = np.tile([1000, 500, 300, 700], 20)
    full = features(f)
    np.testing.assert_allclose(full.effort.iloc[20:], 1)
    prefix = features(f.iloc[:40])
    pd.testing.assert_frame_equal(prefix, full.iloc[:40])
    a = events(full)
    polluted = full.assign(ma8=-1e6, ma13=1e9, ma21=-1e9)
    b = events(polluted)
    pd.testing.assert_frame_equal(a, b[a.columns])
