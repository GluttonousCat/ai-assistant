import numpy as np
import pandas as pd
import pytest

from skills.trend.mtf import features
from skills.trend.reversal import reversal_context
from tools.market.universe import board_of, risk_name


def bars(close):
    close = np.array(close, dtype=float)
    previous = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        dict(
            bar_end=pd.date_range("2026-01-01", periods=len(close), freq="h"),
            open=previous,
            close=close,
            high=np.maximum(previous, close) + 0.05,
            low=np.minimum(previous, close) - 0.05,
            volume=1e6,
            amount=1e8,
        )
    )


def reversal_path():
    return list(np.linspace(14, 10, 80)) + [
        10.5,
        10.6,
        10.7,
        10.8,
        10.9,
        11,
        10.8,
        11.2,
    ]


def test_downward_drift_first_bull_is_observed_but_mature_relaunch_does_not_reset():
    r = reversal_context(features(bars(reversal_path())))
    assert r.iloc[80].reversal_ignition and r.iloc[80].reversal_fresh
    assert r.iloc[80].reversal_kind in ["阴跌反转", "深坑反转"]
    assert r.iloc[
        83
    ].reversal_fresh  # Second through fourth advances remain observable.
    assert not r.iloc[-1].reversal_fresh
    assert r.iloc[-1].reversal_start == r.iloc[80].bar_end
    assert r.reversal_ignition.sum() == 1


def test_uptrend_bull_and_single_random_bull_are_not_low_reversal():
    for path in [np.linspace(10, 14, 100), [10] * 80 + [10.5]]:
        r = reversal_context(features(bars(path)))
        assert not r.reversal_fresh.any()


def test_a_sustained_new_down_leg_can_reverse_above_a_months_old_floor():
    path = list(np.linspace(14, 10, 80)) + [10.5]
    path += list(np.linspace(10.6, 20, 60)) + list(np.linspace(19.9, 15, 40)) + [15.7]
    f = features(bars(path))
    old = reversal_context(f)
    new = reversal_context(f, cycle_reset=True)
    assert old.iloc[-1].reversal_start == old.iloc[80].bar_end
    assert not old.iloc[-1].reversal_fresh
    assert new.iloc[-1].reversal_ignition and new.iloc[-1].reversal_fresh
    assert new.iloc[-1].reversal_floor > old.iloc[80].reversal_floor
    prefix = reversal_context(f.iloc[:-1], cycle_reset=True)
    pd.testing.assert_series_equal(prefix.reversal_start, new.iloc[:-1].reversal_start)
    local = reversal_context(features(bars(reversal_path())), cycle_reset=True)
    assert local.reversal_ignition.sum() == 1


def test_pit_needs_reclaim_not_just_green_body():
    path = [14] * 72 + [13.5, 13, 12.5, 12, 11.5, 11, 10.5, 10, 10.6]
    raw = bars(path)
    r = reversal_context(features(raw))
    assert r.iloc[-1].reversal_ignition and r.iloc[-1].reversal_kind == "深坑反转"
    raw.loc[80, ["open", "close"]] = [9.95, 10.01]
    assert not reversal_context(features(raw)).iloc[-1].reversal_ignition


def test_reversal_is_causal_and_adjustment_scale_invariant():
    raw = bars(reversal_path())
    full = reversal_context(features(raw))
    cols = ["reversal_start", "reversal_fresh", "reversal_age", "reversal_ignition"]
    pd.testing.assert_frame_equal(
        reversal_context(features(raw.iloc[:84]))[cols], full.iloc[:84][cols]
    )
    raw[["open", "high", "low", "close"]] *= 1.23456789
    pd.testing.assert_frame_equal(reversal_context(features(raw))[cols], full[cols])


def test_completed_60f_observation_and_daily_entry_use_same_fresh_origin():
    from skills.trend.mtf import MtfConfig, evaluate, result

    path = list(np.linspace(16, 10, 120)) + [10.5, 10.6, 10.7, 10.8]
    raw = bars(path)
    days = pd.bdate_range("2026-01-01", periods=31)
    raw["bar_end"] = [
        day + pd.Timedelta(hours=h, minutes=m)
        for day in days
        for h, m in [(10, 30), (11, 30), (14, 0), (15, 0)]
    ]
    daily = (
        raw.groupby(raw.bar_end.dt.date)
        .agg(
            dict(
                bar_end="last",
                open="first",
                close="last",
                high="max",
                low="min",
                volume="sum",
                amount="sum",
            )
        )
        .reset_index(drop=True)
    )
    cfg = MtfConfig(fresh_reversal=True, max_risk_pct=20)
    minute, day = evaluate(raw, daily, cfg)
    r = result(day, cfg)
    assert minute.iloc[120].reversal_ignition
    assert r["trend60"]["origin"] == minute.iloc[120].bar_end.isoformat()
    assert r["trend60"]["fresh"] and r["eligible"]
    assert r["pattern"] == "base_rebound"
    _, incomplete = evaluate(raw.iloc[:-1], daily, cfg)
    assert not incomplete.iloc[-1].eligible
    assert not result(incomplete, cfg)["trend60"]["fresh"]
    assert incomplete.iloc[-1].state == "数据不足"
    # More subtle: a current 15:00 exists, but an interior bar is absent and
    # yesterday was fresh. merge_asof must not carry that observation forward.
    following = bars([10.8, 10.75, 10.72, 10.70, 10.68]).iloc[1:].copy()
    next_day = pd.bdate_range(days[-1], periods=2)[-1]
    following["bar_end"] = [
        next_day + pd.Timedelta(hours=h, minutes=m)
        for h, m in [(10, 30), (11, 30), (14, 0), (15, 0)]
    ]
    raw2 = pd.concat([raw, following], ignore_index=True)
    daily2 = (
        raw2.groupby(raw2.bar_end.dt.date)
        .agg(
            dict(
                bar_end="last",
                open="first",
                close="last",
                high="max",
                low="min",
                volume="sum",
                amount="sum",
            )
        )
        .reset_index(drop=True)
    )
    hole = raw2[~raw2.bar_end.eq(next_day + pd.Timedelta(hours=11, minutes=30))]
    _, broken = evaluate(hole, daily2, cfg)
    assert broken.iloc[-2].reversal_fresh
    assert not broken.iloc[-1].context_complete
    assert not result(broken, cfg)["trend60"]["fresh"]


def test_boards_and_entire_st_family():
    assert [
        board_of(c)
        for c in ["600000.SH", "001001.SZ", "300001.SZ", "688001.SH", "920001.BJ"]
    ] == ["主板", "主板", "创业板", "科创板", "北交所"]
    assert board_of("200001.SZ") == "其他"
    assert all(
        risk_name(n) for n in ["ST某某", "*ST某某", "S*ST某某", "SST某某", "退市某某"]
    )
    assert not risk_name("中国核电")


def test_reversal_versions_freeze_old_cycles_and_validate_periods():
    from skills.trend.mtf import (
        MtfConfig,
        VERSION,
        REVERSAL_VERSION,
        FRESH_VERSION,
        version_for,
    )

    assert version_for(MtfConfig()) == VERSION
    assert version_for(MtfConfig(fresh_reversal=True)) == REVERSAL_VERSION
    assert (
        version_for(MtfConfig(fresh_reversal=True, reversal_cycle_reset=True))
        == FRESH_VERSION
    )
    with pytest.raises(ValueError):
        MtfConfig(reversal_cycle_reset=True)
    with pytest.raises(ValueError):
        MtfConfig(fresh_reversal=True, fast=5)
