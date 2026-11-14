from dataclasses import replace
import numpy as np
import pandas as pd
import pytest

from skills.quant.models import BacktestConfig
from skills.quant.reversal_exit import decide
from skills.quant.engine import simulate
from skills.quant.tests.test_engine import fixture, price, CFG


CONFIG = BacktestConfig(exit_policy="reversal", protect_after_r=1, trail_after_r=2)


def position(**patch):
    return dict(
        entry_adjusted=10, initial_risk=1, peak=10.3, stop=9, failure_count=0, **patch
    )


def test_failed_rebound_and_no_instant_m21_exit_during_base():
    assert decide(position(), 9.9, 0.3, 5, CONFIG, 10.02)[1] == "rebound_failed"
    p = position()
    p["failure_count"] = 3
    assert decide(p, 10.1, 0.3, 4, CONFIG, 10.02)[1] is None


def test_cost_floor_needs_a_close_not_only_a_wick_and_stop_never_lowers():
    p = position()
    p["peak"] = 11.5
    assert decide(p, 10.5, 0.5, 2, CONFIG, 10.02)[0] == 9
    stop, _ = decide(p, 11, 0.5, 2, CONFIG, 10.02)
    assert stop == 10.02
    p.update(stop=stop, peak=13)
    stop, _ = decide(p, 12.5, 0.4, 3, CONFIG, 10.02)
    assert stop == 12
    p["stop"] = stop
    assert decide(p, 12.1, 2, 4, CONFIG, 10.02)[0] >= stop


def test_demonstrated_rebound_m21_failure_and_timeout():
    p = position()
    p.update(peak=11.2, failure_count=2)
    assert decide(p, 10.4, 0.5, 4, CONFIG, 10.02)[1] == "trend_break_confirmed"
    assert decide(position(), 10.1, 0.3, 40, CONFIG, 10.02)[1] == "holding_timeout"


def daily_fixture():
    s, _ = fixture()
    s.times = pd.bdate_range("2026-01-05", periods=12) + pd.Timedelta(hours=15)
    s.data["atr"] = np.full(12, 0.2)
    return s, s.times


def test_no_fixed_two_r_cap_and_no_retroactive_raised_stop():
    s, t = daily_fixture()
    cfg = replace(
        CFG,
        exit_policy="reversal",
        protect_after_r=1,
        trail_after_r=2,
        trend_exit=False,
    )
    price(s, 2, "high", 11.2)
    price(s, 2, "close", 11.1)  # Over original 2R target.
    price(s, 2, "low", 9.8)  # Below the NEW stop; it must not sell retrospectively.
    # Slice prices and signals consistently with the shortened calendar.
    s.times = t[:3]
    s.data = {k: v[:3] for k, v in s.data.items()}
    r = simulate([s], s.times, timeframe="1D", cfg=cfg)
    assert not r["trades"] and len(r["open_positions"]) == 1


def test_failure_decision_executes_next_day_open():
    s, t = daily_fixture()
    for i in range(1, 12):
        price(s, i, "close", 9.9)
    cfg = replace(CFG, exit_policy="reversal", protect_after_r=1, trend_exit=False)
    r = simulate([s], t, timeframe="1D", cfg=cfg)
    assert r["trades"][0]["reason"] == "rebound_failed"
    assert r["trades"][0]["exit_time"].startswith(str(t[6].date()))


def test_intraday_period_cannot_be_misread_as_five_days():
    s, t = fixture()
    with pytest.raises(ValueError):
        simulate([s], t, cfg=CONFIG)


def test_m21_must_first_be_reclaimed_before_calling_it_a_break():
    s,t=daily_fixture()
    s.data['trail_a'][:]=12
    for i in range(2,12):
        for key,value in [('open',11.1),('high',11.2),('low',10.9),('close',11.1)]:
            price(s,i,key,value)
    cfg=replace(CFG,exit_policy='reversal',protect_after_r=1,trend_exit=True)
    r=simulate([s],t,timeframe='1D',cfg=cfg)
    assert not r['trades'] and len(r['open_positions'])==1
