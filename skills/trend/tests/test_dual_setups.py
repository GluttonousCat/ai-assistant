import numpy as np
import pandas as pd
import pytest
from skills.trend.strategy import evaluated_bars, screen
from skills.trend.tests.test_strategy import sample


def make_setup(kind, timeframe="60F"):
    f = sample()
    if kind == "pullback":
        close = 20 + np.arange(len(f)) * 0.05
        for col, offset in [
            ("open", -0.015),
            ("high", 0.09),
            ("low", -0.09),
            ("close", 0),
        ]:
            f[col] = close + offset
        moves = [
            -0.08,
            -0.16,
            -0.24,
            -0.32,
            -0.40,
            -0.46,
            -0.50,
            -0.52,
            -0.50,
            -0.49,
            -0.48,
            -0.47,
            -0.32,
            -0.22,
            -0.12,
            -0.02,
            0.08,
            0.18,
        ]
    else:
        close = 30 - np.arange(len(f)) * 0.015
        for col, offset in [
            ("open", 0.015),
            ("high", 0.09),
            ("low", -0.09),
            ("close", 0),
        ]:
            f[col] = close + offset
        moves = ([0.02, -0.02] * 10) + [
            -0.30,
            -0.22,
            -0.12,
            0.02,
            0.10,
            0.18,
            0.26,
            0.34,
        ]
    start = len(f) - len(moves)
    origin = f.close.iloc[start - 1]
    prev = origin
    for i, move in enumerate(moves, start):
        cl = origin + move
        f.loc[i, ["open", "high", "low", "close"]] = [
            prev,
            max(prev, cl) + 0.015,
            min(prev, cl) - 0.015,
            cl,
        ]
        f.loc[i, "volume"] = 1_500_000
        prev = cl
    if timeframe == "1D":
        f["bar_end"] = pd.bdate_range("2025-01-01", periods=len(f)) + pd.Timedelta(
            hours=15
        )
        f["amount"] = 80_000_000
    return f


@pytest.mark.parametrize("timeframe", ["60F", "1D"])
def test_intact_uptrend_pullback_has_its_own_confirmation(timeframe):
    x = evaluated_bars(make_setup("pullback", timeframe), timeframe=timeframe)
    assert x.a_eligible.any()
    chosen = x[x.eligible]
    assert chosen.startup_count.between(2, 4).all()
    for i in chosen.index:
        result = screen(
            make_setup("pullback", timeframe).iloc[: i + 1], timeframe=timeframe
        )
        assert result["eligible"] and all(c["passed"] for c in result["checks"])


@pytest.mark.parametrize("timeframe", ["60F", "1D"])
def test_pit_repair_can_pass_while_daily_trend_is_bearish(timeframe):
    x = evaluated_bars(make_setup("pit", timeframe), timeframe=timeframe)
    candidates = x[x.b_eligible]
    assert len(candidates) > 0
    assert not candidates.daily_trend.any()
    assert candidates.pattern.eq("base_rebound").all()
    assert candidates.startup_evidence.str.contains("收回原支撑").any()
    assert not x.a_eligible.any()


def test_pit_rebound_must_reclaim_prior_platform():
    f = make_setup("pit")
    # A bounce that remains underneath the old platform is not a repair.
    floor = f.close.iloc[-9]
    for i in f.index[-5:]:
        f.loc[i, ["open", "high", "low", "close"]] = [
            floor - 0.17,
            floor - 0.10,
            floor - 0.19,
            floor - 0.12,
        ]
    x = evaluated_bars(f)
    assert not x.tail(5).b_eligible.any()
    assert not x.tail(5).b_watch.any()


@pytest.mark.parametrize("timeframe", ["60F", "1D"])
def test_sideways_drift_repair_does_not_need_a_pit(timeframe):
    f = make_setup("pit", timeframe)
    origin = f.close.iloc[-9]
    prev = origin
    for i, move in zip(
        f.index[-8:], [-0.04, -0.02, -0.03, -0.01, 0.08, 0.16, 0.24, 0.32]
    ):
        cl = origin + move
        f.loc[i, ["open", "high", "low", "close"]] = [
            prev,
            max(prev, cl) + 0.015,
            min(prev, cl) - 0.015,
            cl,
        ]
        prev = cl
    x = evaluated_bars(f, timeframe=timeframe)
    candidates = x[x.b_eligible]
    assert len(candidates) > 0
    assert not candidates.daily_trend.any()
    assert not candidates.startup_evidence.str.contains("形成坑").any()
    assert candidates.startup_evidence.str.contains("底部盘整").all()


@pytest.mark.parametrize("timeframe", ["60F", "1D"])
@pytest.mark.parametrize("kind", ["pullback", "pit"])
def test_two_setup_paths_are_causal(timeframe, kind):
    f = make_setup(kind, timeframe)
    full = evaluated_bars(f, timeframe=timeframe)
    columns = [
        "pattern",
        "eligible",
        "first_watch",
        "startup_start",
        "startup_count",
        "startup_evidence",
        "stop_price",
    ]
    for n in range(len(f) - 6, len(f) + 1):
        prefix = evaluated_bars(f.iloc[:n], timeframe=timeframe)
        pd.testing.assert_series_equal(
            prefix.iloc[-1][columns], full.iloc[n - 1][columns], check_names=False
        )


@pytest.mark.parametrize("direction", [1, -1])
def test_big_green_candle_without_either_setup_is_ignored(direction):
    f = sample(direction=direction)
    f.loc[f.index[-1], ["high", "close"]] += 0.5
    f.loc[f.index[-1], "volume"] *= 2
    r = evaluated_bars(f).iloc[-1]
    assert not r.first_watch and not r.eligible
