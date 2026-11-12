import pandas as pd
from skills.trend.strategy import TrendConfig, evaluated_bars, screen
from skills.trend.tests.test_strategy import sample


def startup_sample():
    f = sample()
    start = len(f) - 12
    origin = f.close.iloc[start - 1]
    # A finite correction, a substantive bullish ignition, then advancing bars.
    moves = [
        -0.06,
        -0.12,
        -0.18,
        -0.24,
        -0.30,
        -0.20,
        -0.08,
        0.04,
        0.16,
        0.28,
        0.40,
        0.52,
    ]
    prev = origin
    for i, move in enumerate(moves, start):
        close = origin + move
        f.loc[i, ["open", "high", "low", "close"]] = [
            prev,
            max(prev, close) + 0.02,
            min(prev, close) - 0.02,
            close,
        ]
        f.loc[i, "volume"] = 1_500_000
        prev = close
    return f, start + 5


def test_seed_is_observation_and_confirmation_only_2_through_4():
    f, seed = startup_sample()
    x = evaluated_bars(f)
    assert x.iloc[seed].startup_count == 1
    assert x.iloc[seed].first_watch and not x.iloc[seed].eligible
    confirmed = x[x.eligible]
    assert len(confirmed) > 0
    assert confirmed.startup_count.between(2, 4).all()
    assert not x.loc[x.startup_count > 4, "eligible"].any()


def test_uninterrupted_mature_uptrend_is_not_a_fresh_start():
    f = sample()
    x = evaluated_bars(f)
    assert not x.eligible.any()
    assert screen(f)["state"] == "等待启动"


def test_one_small_red_candle_does_not_restart_count():
    f, seed = startup_sample()
    row = seed + 2
    prev = f.close.iloc[row - 1]
    f.loc[row, ["open", "high", "low", "close"]] = [
        prev,
        prev + 0.02,
        prev - 0.04,
        prev - 0.02,
    ]
    x = evaluated_bars(f)
    assert x.iloc[row].startup_start == x.iloc[row - 1].startup_start
    assert x.iloc[row].startup_count == x.iloc[row - 1].startup_count
    assert x.iloc[row + 1].startup_count == x.iloc[row].startup_count + 1


def test_every_startup_decision_is_prefix_invariant():
    f, seed = startup_sample()
    x = evaluated_bars(f)
    cols = [
        "startup_start",
        "startup_count",
        "startup_age",
        "startup_efficiency",
        "startup_early",
        "eligible",
        "first_watch",
        "stop_price",
        "risk_pct",
    ]
    for n in range(seed + 1, len(f) + 1):
        pd.testing.assert_series_equal(
            evaluated_bars(f.iloc[:n]).iloc[-1][cols],
            x.iloc[n - 1][cols],
            check_names=False,
        )


def test_giant_chasing_candle_and_weak_volume_do_not_pass():
    f, seed = startup_sample()
    f.loc[seed:, "volume"] = 1
    assert not evaluated_bars(f).eligible.any()
    f, seed = startup_sample()
    row = seed + 1
    f.loc[row, ["high", "close"]] += 5
    x = evaluated_bars(f)
    assert not x.iloc[row].eligible


def test_exhausted_pause_window_cannot_restart_without_new_setup():
    f, seed = startup_sample()
    cfg = TrendConfig(startup_pause_bars=0)
    row = seed + 1
    prev = f.close.iloc[row - 1]
    f.loc[row, ["open", "high", "low", "close"]] = [
        prev,
        prev + 0.01,
        prev - 0.02,
        prev - 0.01,
    ]
    x = evaluated_bars(f, cfg)
    assert not x.iloc[row + 1].eligible
