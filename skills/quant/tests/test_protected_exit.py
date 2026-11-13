from dataclasses import replace

import numpy as np
import pandas as pd

from skills.quant.engine import simulate
from skills.quant.tests.test_engine import fixture, price, CFG


def daily_fixture():
    s, _ = fixture()
    s.times = pd.bdate_range("2026-01-05", periods=7) + pd.Timedelta(hours=15)
    s.data = {key: value[:7].copy() for key, value in s.data.items()}
    s.data["atr"] = np.full(7, 0.2)
    s.data["trail_a"] = np.full(7, 10.0)
    return s, s.times


PROTECTED = replace(CFG, exit_policy="protected", take_profit_r=3, max_holding_bars=60)


def test_raise_stop_at_close_never_applies_peak_to_earlier_low():
    s, t = daily_fixture()
    for field, value in [("high", 10.8), ("low", 9.6), ("close", 10.65)]:
        price(s, 2, field, value)
    s.times = s.times[:3]
    s.data = {k: v[:3] for k, v in s.data.items()}
    r = simulate([s], t[:3], timeframe="1D", cfg=PROTECTED)
    assert not r["trades"] and len(r["open_positions"]) == 1
    assert r["open_positions"][0]["pending_exit"] is None


def test_next_day_protected_stop_preserves_profit_and_original_risk():
    s, t = daily_fixture()
    for field, value in [("high", 10.8), ("low", 9.6), ("close", 10.65)]:
        price(s, 2, field, value)
    for field, value in [
        ("open", 10.5),
        ("high", 10.6),
        ("low", 10.2),
        ("close", 10.3),
    ]:
        price(s, 3, field, value)
    r = simulate([s], t, timeframe="1D", cfg=PROTECTED)
    trade = r["trades"][0]
    assert trade["reason"] == "protected_stop" and trade["pnl"] > 0
    assert trade["initial_stop"] == 9.5 and trade["exit_stop"] > 10
    assert (
        pd.Timestamp(trade["exit_time"]).date()
        > pd.Timestamp(trade["entry_time"]).date()
    )
    assert r["metrics"]["ending_equity"] == CFG.initial_cash + trade["pnl"]


def test_one_close_below_line_does_not_exit_the_protected_policy():
    s, t = daily_fixture()
    price(s, 2, "close", 9.9)
    price(s, 3, "close", 10.1)
    r = simulate([s], t, timeframe="1D", cfg=replace(PROTECTED, trend_exit=True))
    assert not r["trades"] and r["open_positions"][0]["pending_exit"] is None


def test_protected_policy_still_honors_hard_structure_stop():
    s, t = daily_fixture()
    price(s, 2, "low", 9.4)
    r = simulate([s], t, timeframe="1D", cfg=PROTECTED)
    assert r["trades"][0]["reason"] == "stop_loss"
    assert r["trades"][0]["exit_price"] == 9.5


def test_raised_protection_does_not_sell_when_next_bar_remains_above_stop():
    s, t = daily_fixture()
    for i, values in [
        (2, (10.0, 10.8, 9.6, 10.65)),
        (3, (10.6, 10.8, 10.5, 10.7)),
        (4, (10.7, 10.9, 10.6, 10.8)),
    ]:
        for key, value in zip(["open", "high", "low", "close"], values):
            price(s, i, key, value)
    s.times = s.times[:5]
    s.data = {k: v[:5] for k, v in s.data.items()}
    r = simulate([s], t[:5], timeframe="1D", cfg=PROTECTED)
    assert not r["trades"] and len(r["open_positions"]) == 1
    assert r["open_positions"][0]["pending_exit"] is None


def test_risk_budget_limits_position_in_addition_to_cash_cap():
    s, t = daily_fixture()
    cfg = replace(
        PROTECTED, initial_cash=100_000, position_weight=0.9, risk_budget_pct=0.1
    )
    r = simulate([s], t, timeframe="1D", cfg=cfg)
    holding = r["open_positions"][0]
    assert holding["entry_notional"] <= 2000
    assert all(point["cash"] >= 0 for point in r["equity"])


def test_target_on_entry_day_cannot_sell_before_t1():
    s, t = daily_fixture()
    price(s, 1, "high", 12.0)
    price(s, 2, "open", 10.3)
    r = simulate([s], t, timeframe="1D", cfg=PROTECTED)
    assert (
        not r["trades"]
        or pd.Timestamp(r["trades"][0]["exit_time"]).date()
        > pd.Timestamp(r["trades"][0]["entry_time"]).date()
    )
