from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from skills.quant.registry import catalog
from skills.quant.support_protection import SupportProtection
from skills.quant.tests.test_tranches import fixture, config
from skills.quant.tranche_engine import simulate
from skills.quant.wyckoff import prepare_wyckoff
from skills.quant.tests.test_stop_orders import native


def volume_stock():
    stock, clock = fixture([10, 10.15, 10.25, 10.35, 10.4, 10.3])
    data = dict(stock.data)
    for k in ["demand_ready", "attack"]:
        data["wyckoff_" + k] = np.ones(6, dtype=bool)
    for k in ["supply_warning", "supply_defense", "structure_failed"]:
        data["wyckoff_" + k] = np.zeros(6, dtype=bool)
    data["confirmed_higher_low"] = np.array([np.nan, 10.08, 10.18, 10.28, 10.3, 10.3])
    data["higher_low_origin"] = np.array(
        [(t.normalize() + pd.Timedelta(hours=10, minutes=30)).value for t in clock]
    )
    data["higher_low_known"] = np.array(
        [(t.normalize() + pd.Timedelta(hours=14)).value for t in clock]
    )
    # These are hostile legacy MA states; volume management must ignore them.
    data["tranche_trend_failed"] = np.ones(6, dtype=bool)
    data["trail_a"] = data["trail_b"] = np.full(6, 9.0)
    return replace(stock, data=data), clock


def test_volume_defense_sells_one_planned_tranche_next_open_and_locks_adds():
    stock, clock = volume_stock()
    stock.data["wyckoff_supply_defense"][2] = True
    stock.data["wyckoff_supply_warning"][2] = True
    stock.data["wyckoff_demand_ready"][2] = False
    stock.data["scale_in_ready"][2:] = False
    plan = config(entry=(0.5, 0.3, 0.2), exits=(0.5, 0.3, 0.2))
    r = simulate(
        [stock], clock, "wyckoff_60f_v31", plan, wyckoff_policy=SupportProtection()
    )
    p = (r["trades"] + r["open_positions"])[0]
    sells = [f for f in p["fills"] if f["side"] == "sell"]
    assert sells and sells[0]["reason"] == "wyckoff_supply_defense"
    assert pd.Timestamp(sells[0]["time"]).date() == clock[3].date()
    assert not any(
        f["side"] == "buy" and pd.Timestamp(f["time"]).date() > clock[2].date()
        for f in p["fills"]
    )
    assert r["execution_audit"]["wyckoff_defensive_orders"] == 1


def test_volume_strategy_is_not_exposed_as_an_unsupported_workspace_template():
    assert "wyckoff_60f_v31" not in {row["id"] for row in catalog()}


def test_volume_strategy_cannot_silently_run_with_legacy_ma_execution():
    from skills.quant.engine import simulate as legacy_simulate

    stock, clock = volume_stock()
    with pytest.raises(ValueError, match="显式"):
        simulate([stock], clock, "wyckoff_60f_v31", config())
    with pytest.raises(ValueError, match="显式"):
        legacy_simulate([stock], clock, "wyckoff_60f_v31")


def test_ma_failure_cannot_force_volume_strategy_to_exit():
    stock, clock = volume_stock()
    stock.data["confirmed_higher_low"][:] = np.nan
    plan = config()
    plan = replace(
        plan, base=replace(plan.base, trend_exit=True, min_trend_holding_bars=1)
    )
    r = simulate(
        [stock], clock, "wyckoff_60f_v31", plan, wyckoff_policy=SupportProtection()
    )
    assert not r["trades"]
    assert r["open_positions"]


def test_mixed_campaigns_keep_separate_exit_rules_and_priority_units():
    trend, clock = volume_stock()
    trend.data["wyckoff_atr"] = trend.data["atr"].copy()
    rebound = replace(
        trend,
        code="000001.SZ",
        data={key: value.copy() for key, value in trend.data.items()},
        signals=[{**trend.signals[0], "family": "base_rebound", "score": 100}],
    )
    rebound.data["wyckoff_structure_failed"][3] = True
    plan = config(entry=(1,), exits=(1,))
    plan = replace(
        plan, base=replace(plan.base, trend_exit=True, min_trend_holding_bars=1)
    )
    kwargs = dict(
        wyckoff_policy=SupportProtection(), wyckoff_families=("base_rebound",)
    )
    constrained = simulate([rebound, trend], clock, "wyckoff_60f_v31", plan, **kwargs)
    assert [t["ts_code"] for t in constrained["trades"]] == [trend.code]
    both = simulate(
        [rebound, trend],
        clock,
        "wyckoff_60f_v31",
        replace(plan, base=replace(plan.base, max_positions=2)),
        **kwargs,
    )
    assert {t["family"]: t["reason"] for t in both["trades"]} == {
        "trend_pullback": "structure_failed",
        "base_rebound": "wyckoff_support_failed",
    }
    second_rebound = replace(rebound, code="000002.SZ")
    capped = simulate(
        [rebound, second_rebound, trend],
        clock,
        "wyckoff_60f_v31",
        replace(plan, base=replace(plan.base, max_positions=3)),
        wyckoff_slot_limit=1,
        **kwargs,
    )
    assert {t["ts_code"] for t in capped["trades"]} == {trend.code, rebound.code}
    assert capped["execution_audit"]["wyckoff_capacity_rejected"] == 1


def test_supply_warning_pauses_adds_without_requiring_a_defensive_sale():
    stock, clock = volume_stock()
    stock.data["wyckoff_supply_warning"][1] = True
    stock.data["wyckoff_demand_ready"][1] = False
    r = simulate(
        [stock], clock, "wyckoff_60f_v31", config(), wyckoff_policy=SupportProtection()
    )
    p = (r["trades"] + r["open_positions"])[0]
    buys = [f for f in p["fills"] if f["side"] == "buy"]
    assert not any(pd.Timestamp(f["time"]).date() == clock[2].date() for f in buys)
    assert len(buys) > 1
    assert not any(f["reason"] == "wyckoff_supply_defense" for f in p["fills"])


def test_daily_red_candle_does_not_gate_a_completed_native_volume_setup(monkeypatch):
    stock, clock = fixture([10, 10, 10])
    minute = native(stock)
    stock.data["open"][0] = stock.data["adjusted_open"][0] = 11
    stock.data["high"][0] = stock.data["adjusted_high"][0] = 11.1
    minute.loc[0, "open"] = 11
    minute.loc[0, "high"] = 11.1

    def known(f, cfg):
        f = f.copy()
        f["atr"] = 0.1
        f["support"] = 9.8
        f["support_origin"] = f.bar_end.iloc[0]
        f["support_known"] = f.bar_end.iloc[1]
        for key in ["supply_warning", "supply_defense", "structure_failed"]:
            f[key] = False
        f["demand_ready"] = True
        f["attack"] = False
        f["event"] = ""
        f["score"] = 0.0
        f["event_floor"] = 9.8
        f["event_origin"] = f.bar_end.iloc[0]
        f["contraction"] = 0.5
        f["effort"] = 1.2
        f["close_location"] = 0.8
        f.loc[1, ["attack", "event", "score"]] = [True, "lps", 80]
        return f

    monkeypatch.setattr("skills.quant.wyckoff.events", known)
    before = stock.data["open"].copy()
    result = prepare_wyckoff(minute, stock)
    assert len(result.signals) == 1 and result.signals[0]["i"] == 0
    assert (
        result.signals[0]["evidence"]["native_event_time"]
        == minute.bar_end.iloc[1].isoformat()
    )
    assert result.signals[0]["score"] == 80
    assert result.data["scale_in_ready"][0]
    np.testing.assert_array_equal(stock.data["open"], before)
    assert not prepare_wyckoff(minute.drop(index=3), stock).signals
    faded = minute.copy()
    faded.loc[3, "close"] = 9.9
    lost = prepare_wyckoff(faded, stock)
    assert not lost.signals and not lost.data["scale_in_ready"][0]
