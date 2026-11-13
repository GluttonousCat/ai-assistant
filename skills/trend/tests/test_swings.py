"""Parent swing is causal context, independent of a local trade permission."""

from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skills.trend import service
from skills.trend.strategy import TrendConfig, evaluated_bars, screen
from skills.trend.swings import add_swing_context


def jiuzhou(timeframe):
    # Native bars rebased to 2026-11-12, not a synthetic reconstruction of day K.
    f = pd.read_csv(Path(__file__).parent / "fixtures/jiuzhou_20261112.csv")
    return f.loc[f.timeframe.eq(timeframe)].reset_index(drop=True)


@pytest.mark.parametrize("timeframe", ["60F", "1D"])
def test_jiuzhou_prior_launch_survives_pullback_without_changing_entries(timeframe):
    f = jiuzhou(timeframe)
    current = evaluated_bars(f, timeframe=timeframe)
    legacy = evaluated_bars(
        f, replace(TrendConfig(), track_swing_context=False), timeframe
    )
    for name in ["a_eligible", "b_eligible", "eligible", "stop_price", "startup_count"]:
        pd.testing.assert_series_equal(current[name], legacy[name])
    if timeframe == "60F":
        assert legacy.iloc[-1].first_watch
        assert current.iloc[-1].resume_watch and not current.iloc[-1].first_watch
        assert current.iloc[-1].swing_start == pd.Timestamp("2026-11-04 14:00")
        assert current.iloc[-1].startup_start == pd.Timestamp("2026-11-12 15:00")
        # Both A's intervening pullback and B's fresh local seed share the parent.
        middle = current[current.bar_end.eq("2026-11-10 10:30")].iloc[0]
        assert middle.a_setup_phase == "pullback_resume"
        assert middle.a_swing_start == current.iloc[-1].b_swing_start
        assert screen(f)["state"] == "回调再起观察"
    else:
        background = current[current.bar_end.eq("2026-11-04 15:00")].iloc[0]
        assert background.swing_ignition
        assert not background.eligible and not background.first_watch
        assert background.background_swing_start == background.bar_end


@pytest.mark.parametrize("timeframe", ["60F", "1D"])
def test_phase_and_parent_are_prefix_consistent(timeframe):
    f = jiuzhou(timeframe)
    full = evaluated_bars(f, timeframe=timeframe)
    cols = [
        "setup_phase",
        "swing_start",
        "swing_ignition",
        "first_watch",
        "resume_watch",
    ]
    for count in range(len(f) - 20, len(f) + 1):
        prefix = evaluated_bars(f.iloc[:count], timeframe=timeframe)
        pd.testing.assert_frame_equal(prefix[cols], full.iloc[:count][cols])


def context_frame():
    f = pd.DataFrame(
        {
            "bar_end": pd.date_range("2026-01-01", periods=24, freq="h"),
            "open": 10.5,
            "close": 10.0,
            "low": 9.8,
            "high": 10.6,
            "atr": 1.0,
            "ema_slow": 10.0,
            "pattern": "base_rebound",
            "first_watch": False,
        }
    )
    f.loc[10, ["open", "close", "low", "high"]] = [10.0, 12.0, 9.8, 12.1]
    for key in ["a", "b"]:
        f[f"{key}_startup_start"] = pd.NaT
    f.loc[12:14, "a_startup_start"] = f.bar_end.iloc[12]
    f.loc[12:14, "pattern"] = "trend_pullback"
    f.loc[15:17, "b_startup_start"] = f.bar_end.iloc[15]
    f.loc[18, "low"] = 8.0  # Parent floor fails, not merely a local pause.
    f.loc[20:, "b_startup_start"] = f.bar_end.iloc[20]
    f.loc[[12, 15, 20], "first_watch"] = True
    return f


def test_engulfing_background_crosses_families_but_broken_floor_allows_new_first():
    x = add_swing_context(context_frame(), TrendConfig())
    assert x.iloc[10].swing_ignition and not x.iloc[10].first_watch
    for i in [12, 15]:
        assert x.iloc[i].resume_watch and not x.iloc[i].first_watch
        assert x.iloc[i].swing_start == x.bar_end.iloc[10]
    assert pd.isna(x.iloc[18].background_swing_start)
    assert x.iloc[20].first_watch and not x.iloc[20].resume_watch
    assert x.iloc[20].swing_start == x.bar_end.iloc[20]


def test_sustained_deep_weakness_can_end_parent_without_touching_original_floor():
    f = context_frame()
    f.loc[18, "low"] = 9.8
    f.loc[16, "high"] = 15.0
    f.loc[18, ["open", "close", "low", "high", "ema_slow"]] = [11, 10, 9.8, 11.1, 12]
    x = add_swing_context(f, TrendConfig())
    assert pd.isna(x.iloc[18].background_swing_start)
    assert x.iloc[20].first_watch


def test_old_v4_snapshot_keeps_original_chart_interpretation(monkeypatch):
    f = jiuzhou("60F").assign(adj_factor=1.0)
    monkeypatch.setattr(service, "adjusted_bars", lambda *args: f.copy())
    config = asdict(TrendConfig())
    config.pop("track_swing_context")
    old = service.stock_detail("603456.SH", "2026-01-01", "2026-11-12T15:00:00", config)
    current = service.stock_detail(
        "603456.SH", "2026-01-01", "2026-11-12T15:00:00", asdict(TrendConfig())
    )
    assert old["data"][-1]["first_watch"]
    assert "setup_phase" not in old["data"][-1]
    assert current["data"][-1]["resume_watch"]
    assert not current["data"][-1]["first_watch"]


@pytest.mark.parametrize("value", [0, -1, np.inf, np.nan])
def test_swing_thresholds_reject_non_positive_or_non_finite(value):
    with pytest.raises(ValueError):
        TrendConfig(swing_break_atr=value)
