from dataclasses import replace
import numpy as np
import pandas as pd
import pytest
from skills.quant.engine import simulate, opening_time
from skills.quant.models import BacktestConfig
from skills.quant.prepare import MarketSeries


CFG = BacktestConfig(
    initial_cash=10_000,
    max_positions=1,
    position_weight=0.99,
    slippage=0,
    participation=1,
    trend_exit=False,
)


def fixture(code="000001.SZ"):
    times = pd.DatetimeIndex(
        [
            day + pd.Timedelta(hours=h, minutes=m)
            for day in pd.bdate_range("2026-01-05", periods=3)
            for h, m in [(10, 30), (11, 30), (14, 0), (15, 0)]
        ]
    )
    values = {
        "open": 10.0,
        "high": 10.2,
        "low": 9.8,
        "close": 10.0,
        "volume": 100_000.0,
        "amount": 1_000_000.0,
        "scale": 1.0,
        "up_limit": 11.0,
        "down_limit": 9.0,
        "trail_a": 9.0,
        "trail_b": 9.0,
    }
    data = {k: np.full(len(times), v) for k, v in values.items()}
    for k in ["open", "high", "low", "close"]:
        data["adjusted_" + k] = data[k].copy()
    signal = {
        "i": 0,
        "family": "trend_pullback",
        "preferred": True,
        "leg": times[0].isoformat(),
        "count": 2,
        "stop": 9.5,
        "score": 90.0,
    }
    return MarketSeries(
        code, code, pd.Timestamp("2026-11-12T17:08:00"), times, data, [signal]
    ), times


def price(s, i, field, value):
    s.data[field][i] = value
    s.data["adjusted_" + field][i] = value * s.data["scale"][i]


def test_next_bar_execution_and_t1_defer_a_stop():
    s, t = fixture()
    price(s, 1, "open", 10.1)
    price(s, 2, "low", 9.4)
    price(s, 4, "open", 9.6)
    r = simulate([s], t, cfg=CFG)
    trade = r["trades"][0]
    assert trade["entry_price"] == 10.1
    assert trade["signal_time"] == t[0].isoformat()
    assert trade["entry_time"] == opening_time(t[1], "60F").isoformat()
    assert (
        pd.Timestamp(trade["exit_time"]).date()
        > pd.Timestamp(trade["entry_time"]).date()
    )
    assert trade["exit_price"] == 9.6 and trade["reason"] == "t1_deferred_stop"
    assert r["execution_audit"]["t1_stop_deferred"] >= 1


def test_limit_up_cannot_be_bought():
    s, t = fixture()
    price(s, 1, "open", 11)
    r = simulate([s], t, cfg=CFG)
    assert not r["trades"] and not r["open_positions"]
    assert r["execution_audit"]["entry_limit_up_rejected"] == 1


def test_zero_volume_bar_cannot_fill_an_order():
    s, t = fixture()
    s.data["volume"][1] = 0
    r = simulate([s], t, cfg=CFG)
    assert not r["open_positions"] and not r["trades"]


def test_limit_down_defers_exit_until_a_tradable_bar():
    s, t = fixture()
    price(s, 2, "low", 9.4)
    price(s, 4, "open", 9)
    price(s, 5, "open", 9.2)
    r = simulate([s], t, cfg=CFG)
    assert r["trades"][0]["exit_time"] == opening_time(t[5], "60F").isoformat()
    assert r["trades"][0]["exit_price"] == 9.2
    assert r["execution_audit"]["exit_limit_down_deferred"] >= 1


def test_intrabar_ambiguous_bracket_takes_stop_first():
    s, t = fixture()
    price(s, 4, "low", 9.4)
    price(s, 4, "high", 11.2)
    r = simulate([s], t, cfg=CFG)
    assert r["trades"][0]["exit_price"] == 9.5
    assert r["execution_audit"]["ambiguous_bracket_stop_first"] == 1


def test_profit_gap_at_open_precedes_later_intrabar_stop():
    s, t = fixture()
    price(s, 4, "open", 11.1)
    price(s, 4, "high", 11.2)
    price(s, 4, "low", 9.4)
    r = simulate([s], t, cfg=CFG)
    assert r["trades"][0]["exit_price"] == 11.1
    assert r["trades"][0]["reason"] == "take_profit"


def test_cash_and_slot_constraints_are_shared_across_stocks():
    a, t = fixture("000001.SZ")
    b, _ = fixture("000002.SZ")
    r = simulate([b, a], t, cfg=CFG)
    assert len(r["open_positions"]) == 1
    assert r["open_positions"][0]["ts_code"] == "000001.SZ"
    assert all(x["cash"] >= 0 and x["positions"] <= 1 for x in r["equity"])


def test_fees_slippage_and_final_mark_are_not_free():
    s, t = fixture()
    r = simulate([s], t, cfg=replace(CFG, slippage=0.001, max_holding_bars=1))
    trade = r["trades"][0]
    assert trade["pnl"] < 0 and trade["fees"] > 0
    assert r["metrics"]["total_return_pct"] < 0
    assert r["metrics"]["total_fees"] == pytest.approx(trade["fees"])
    assert r["metrics"]["ending_equity"] == pytest.approx(
        CFG.initial_cash + trade["pnl"]
    )


def test_future_observed_membership_cannot_trade_in_strict_mode():
    s, t = fixture()
    r = simulate([s], t, cfg=CFG, universe_mode="observed_only")
    assert not r["trades"] and not r["open_positions"]
    assert r["execution_audit"]["before_membership_observed"] == 1


def test_missing_next_bar_does_not_buy_at_later_resumption():
    s, t = fixture()
    s.times = s.times.delete(1)
    s.data = {k: np.delete(v, 1) for k, v in s.data.items()}
    r = simulate([s], t, cfg=CFG)
    assert not r["open_positions"]
    assert r["execution_audit"]["no_next_bar"] == 1


def test_no_forced_sale_on_last_bar_and_no_same_leg_reentry():
    s, t = fixture()
    s.signals.append({**s.signals[0], "i": 5, "count": 3})
    price(s, 4, "high", 11.2)
    r = simulate([s], t, cfg=CFG)
    assert len(r["trades"]) == 1 and not r["open_positions"]
    s, t = fixture()
    r = simulate([s], t, cfg=CFG)
    assert len(r["open_positions"]) == 1 and not r["trades"]


def test_factor_change_keeps_economic_value_in_factor_units():
    s, t = fixture()
    for i in range(4, len(t)):
        s.data["scale"][i] = 2
        s.data["up_limit"][i] = 5.5
        s.data["down_limit"][i] = 4.5
        for key in ["open", "high", "low", "close"]:
            price(s, i, key, s.data[key][i] / 2)
    r = simulate([s], t, cfg=replace(CFG, max_holding_bars=4))
    assert len(r["trades"]) == 1
    assert r["trades"][0]["exit_price"] == 5
    assert abs(r["trades"][0]["pnl"]) < 20


@pytest.mark.parametrize(
    "field,value",
    [
        ("initial_cash", 0),
        ("slippage", float("nan")),
        ("position_weight", 2),
        ("participation", 0),
        ("max_positions", 1.5),
        ("max_holding_bars", 1.5),
    ],
)
def test_invalid_execution_parameters_are_rejected(field, value):
    with pytest.raises(ValueError):
        replace(CFG, **{field: value})
