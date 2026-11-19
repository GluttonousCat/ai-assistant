from dataclasses import replace

import pandas as pd
import pytest

from skills.quant.stop_orders import StopBar, prepare_stop_bars, stop_reference
from tests.skills.quant.test_tranches import config, fixture
from skills.quant.tranche_engine import simulate


def native(stock):
    rows = []
    for i, day in enumerate(stock.times.normalize()):
        for hour, minute in [(10, 30), (11, 30), (14, 0), (15, 0)]:
            rows.append(
                dict(
                    bar_end=day + pd.Timedelta(hours=hour, minutes=minute),
                    adjustment="none",
                    **{
                        k: stock.data[k][i]
                        for k in ("open", "high", "low", "close", "volume")
                    },
                )
            )
    return pd.DataFrame(rows)


def test_gap_touches_and_activated_orders_never_guarantee_the_stop_price():
    end = pd.Timestamp("2026-01-06T10:30")
    assert stop_reference(StopBar(end, 9, 8.8, 1e6), 9.8) == 9
    assert stop_reference(StopBar(end, 10, 9.7, 1e6), 9.8) == 9.8
    assert stop_reference(StopBar(end, 10, 9.9, 1e6), 9.8) is None
    assert stop_reference(StopBar(end, 10, 9.9, 1e6), 9.8, True) == 10


def test_new_lots_stay_t1_locked_after_a_same_day_stop_trigger():
    stock, clock = fixture([10, 9.7, 9.6, 9.5])
    stops = {stock.code: prepare_stop_bars(stock, native(stock))}
    result = simulate([stock], clock, "swing_60f_v8", config(), stop_bars=stops)
    trade = result["trades"][0]
    assert (
        pd.Timestamp(trade["exit_time"]).date()
        > pd.Timestamp(trade["entry_time"]).date()
    )
    assert trade["exit_price"] == 9.7
    assert result["execution_audit"]["t1_exit_deferred"] > 0
    assert trade["reason"] == "preplaced_protective_stop"


def test_native_stops_respect_liquidity_and_sell_limit_instead_of_phantom_fills():
    stock, clock = fixture([10, 10, 9.7, 9.5, 9.4])
    # No trigger on the acquisition day; the second session breaches the stop.
    stock.data["low"][2] = stock.data["adjusted_low"][2] = 9.7
    stock.data["open"][2] = stock.data["adjusted_open"][2] = 9.8
    stock.data["down_limit"][2] = 9.8
    frame = native(stock)
    bars = prepare_stop_bars(stock, frame)
    stops = {stock.code: bars}
    result = simulate([stock], clock, "swing_60f_v8", config(), stop_bars=stops)
    trade = result["trades"][0]
    assert pd.Timestamp(trade["exit_time"]).normalize() > clock[2].normalize()
    assert result["execution_audit"]["exit_limit_down_deferred"] >= 1
    thin = {
        day: tuple(replace(b, volume=0) for b in values) for day, values in bars.items()
    }
    result = simulate(
        [stock], clock, "swing_60f_v8", config(), stop_bars={stock.code: thin}
    )
    assert all(t["reason"] != "preplaced_protective_stop" for t in result["trades"])
    assert all("resolution" not in f for t in result["trades"] for f in t["fills"])
    assert result["execution_audit"]["resting_stop_liquidity_deferred"] >= 1
    assert result["execution_audit"].get("resting_stop_activations", 0) == 0


def test_missing_or_adjusted_minute_data_are_rejected():
    stock, _ = fixture([10, 10, 10])
    frame = native(stock)
    with pytest.raises(ValueError, match="完整"):
        prepare_stop_bars(stock, frame.iloc[1:])
    frame["adjustment"] = "qfq"
    with pytest.raises(ValueError, match="未复权"):
        prepare_stop_bars(stock, frame)


def test_explicit_daily_fallback_preserves_raw_prices_and_labels_resolution():
    stock, _ = fixture([10, 10, 10])
    frame = native(stock)
    frame.loc[0, "low"] = 9.5
    with pytest.raises(ValueError, match="价格不一致"):
        prepare_stop_bars(stock, frame)
    bars = prepare_stop_bars(stock, frame, allow_daily_fallback=True)
    fallback = bars[stock.times[0].date()]
    assert len(fallback) == 1 and fallback[0].resolution == "1D_fallback"
    assert fallback[0].low == stock.data["low"][0] == 9.9
    assert frame.loc[0, "low"] == 9.5


def test_todays_profit_cannot_raise_the_stop_before_todays_native_lows():
    stock, clock = fixture([10, 10, 10.7, 10.7])
    stock.data["low"][2] = stock.data["adjusted_low"][2] = 9.9
    result = simulate(
        [stock],
        clock,
        "swing_60f_v8",
        config(),
        stop_bars={stock.code: prepare_stop_bars(stock, native(stock))},
    )
    assert result["execution_audit"].get("resting_stop_activations", 0) == 0


def test_intraday_stop_proceeds_cannot_fund_another_stocks_opening_purchase():
    a, clock = fixture([10, 10, 9.7, 9.7])
    b, _ = fixture([10, 10, 10, 10])
    b = replace(
        b,
        code="600001.SH",
        signals=[{**b.signals[0], "i": 1, "leg": clock[1].isoformat()}],
    )
    plan = config(entry=(1.0,), exits=(1.0,))
    plan = replace(
        plan,
        base=replace(
            plan.base,
            initial_cash=5000,
            position_weight=1,
            risk_budget_pct=5,
            max_positions=2,
        ),
    )
    stops = {s.code: prepare_stop_bars(s, native(s)) for s in [a, b]}
    result = simulate([a, b], clock, "swing_60f_v8", plan, stop_bars=stops)
    assert result["execution_audit"]["campaigns_started"] == 1
    assert {t["ts_code"] for t in result["trades"]} == {a.code}
