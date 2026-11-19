from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from skills.quant.models import BacktestConfig
from skills.quant.prepare import MarketSeries
from skills.quant.tranches import Inventory, TrancheConfig
from skills.quant.tranche_engine import simulate


def fixture(prices):
    close = np.asarray(prices, dtype=float)
    n = len(close)
    times = pd.bdate_range("2026-01-05", periods=n) + pd.Timedelta(hours=15)
    op = np.r_[close[0], close[:-1]]
    data = dict(
        open=op.copy(),
        high=np.maximum(op, close) + 0.1,
        low=np.minimum(op, close) - 0.1,
        close=close.copy(),
        scale=np.ones(n),
        volume=np.full(n, 1e8),
        amount=np.full(n, 1e9),
        up_limit=np.full(n, 100.0),
        down_limit=np.full(n, 1.0),
        atr=np.full(n, 0.1),
        trail_a=np.full(n, 9.0),
        trail_b=np.full(n, 9.0),
        scale_in_ready=np.ones(n, dtype=bool),
        tranche_trend_failed=np.zeros(n, dtype=bool),
    )
    for k in ["open", "high", "low", "close"]:
        data["adjusted_" + k] = data[k].copy()
    sig = dict(
        i=0,
        family="trend_pullback",
        preferred=True,
        leg=times[0].isoformat(),
        count=1,
        stop=9.8,
        score=90,
    )
    return MarketSeries("600000.SH", "样本", times[0], times, data, [sig]), times


def config(entry=(0.4, 0.4, 0.2), exits=(0.4, 0.4, 0.2)):
    base = BacktestConfig(
        close_only=True,
        initial_cash=1_000_000,
        max_positions=1,
        position_weight=0.1,
        risk_budget_pct=0.35,
        max_entry_risk_pct=4,
        slippage=0,
        trend_exit=False,
        max_holding_bars=8,
        commission_rate=0,
        minimum_commission=0,
        stamp_tax=0,
        transfer_fee=0,
    )
    return TrancheConfig(
        base=base,
        entry_weights=entry,
        exit_weights=exits,
        add_at_r=(0.5, 1.0) if len(entry) == 3 else (),
        take_profit_pct=(5.0, 8.0) if len(exits) == 3 else (),
    )


def test_fifo_cost_and_lot_settlement_are_independent():
    inv = Inventory()
    d1 = date(2026, 1, 5)
    d2 = date(2026, 1, 6)
    inv.buy(100, 1001, d1)
    inv.buy(100, 1101, d2)
    assert inv.available(d2) == 100
    with pytest.raises(ValueError):
        inv.sell(101, 1100, d2)
    pnl = inv.sell(50, 550, d2)
    assert pnl == pytest.approx(49.5)
    assert inv.cost == pytest.approx(1601.5)
    assert inv.realized == pytest.approx(pnl)


def test_three_sell_legs_form_one_closed_campaign_and_conserve_cash():
    s, t = fixture([10, 10.1, 10.2, 10.6, 10.9, 11.1, 11.2, 11.0, 10.9, 10.8, 10.7])
    r = simulate([s], t, "swing_60f_v8", config())
    assert len(r["trades"]) == 1
    trade = r["trades"][0]
    assert trade["buy_legs"] == 3 and trade["sell_legs"] == 3
    assert len(trade["fills"]) == 6
    assert r["metrics"]["ending_equity"] == pytest.approx(1_000_000 + trade["pnl"])
    assert r["metrics"]["closed_trades"] == 1
    assert all(
        pd.Timestamp(f["time"]).date() > pd.Timestamp(trade["entry_time"]).date()
        for f in trade["fills"]
        if f["side"] == "sell"
    )


def test_partial_realized_pnl_of_open_campaign_is_not_lost_or_counted_twice():
    s, t = fixture([10, 10.15, 10.3, 10.6, 10.9, 11.1])
    r = simulate([s], t, "swing_60f_v8", config())
    assert not r["trades"] and len(r["open_positions"]) == 1
    p = r["open_positions"][0]
    assert p["partial_realized_pnl"] > 0 and p["sell_legs"] >= 1
    assert r["metrics"]["ending_equity"] == pytest.approx(
        1_000_000 + p["partial_realized_pnl"] + p["unrealized_pnl"]
    )


def test_losing_probe_never_averages_down_or_completes_a_plan():
    s, t = fixture([10, 9.9, 9.7, 9.6, 9.5])
    r = simulate([s], t, "swing_60f_v8", config())
    assert len(r["trades"]) == 1
    assert r["trades"][0]["buy_legs"] == 1 and r["trades"][0]["reason"] == "stop_loss"
    assert r["trades"][0]["entry_notional"] <= 40_000


def test_gap_below_stop_overrides_a_pending_partial_profit_exit():
    s, t = fixture([10, 10.2, 10.6, 9.5, 9.4, 9.3])
    s.data["open"][3] = s.data["adjusted_open"][3] = 9.5
    r = simulate([s], t, "swing_60f_v8", config(entry=(1.0,)))
    assert r["trades"][0]["reason"] in ("stop_loss", "protected_stop")
    assert r["trades"][0]["sell_legs"] == 1


def test_invalid_weight_or_trigger_plan_is_rejected():
    with pytest.raises(ValueError):
        replace(config(), entry_weights=(0.4, 0.4, 0.3))
    with pytest.raises(ValueError):
        replace(config(), add_at_r=(1.0, 0.5))


@pytest.mark.parametrize(
    "weights,expected",
    [((0.4, 0.4, 0.2), [4000, 4000, 2000]), ((0.5, 0.3, 0.2), [5000, 3000, 2000])],
)
def test_exit_weights_reference_original_built_position_not_remaining(
    weights, expected
):
    s, t = fixture([10, 10.1, 10.2, 10.6, 10.9, 11.1, 11.2, 11.0, 10.9, 10.8, 10.7])
    r = simulate([s], t, "swing_60f_v8", config(entry=(1.0,), exits=weights))
    assert [
        f["quantity"] for f in r["trades"][0]["fills"] if f["side"] == "sell"
    ] == expected


def test_fees_and_partial_realized_cash_reconcile_with_open_inventory():
    s, t = fixture([10, 10.1, 10.2, 10.7, 11.0, 11.2])
    cfg = config()
    cfg = replace(
        cfg,
        base=replace(
            cfg.base,
            commission_rate=0.0003,
            minimum_commission=5,
            stamp_tax=0.0005,
            slippage=0.0005,
        ),
    )
    r = simulate([s], t, "swing_60f_v8", cfg)
    p = r["open_positions"][0]
    fills = p["fills"]
    cash = (
        1_000_000
        - sum(f["notional"] + f["fee"] for f in fills if f["side"] == "buy")
        + sum(f["notional"] - f["fee"] for f in fills if f["side"] == "sell")
    )
    assert r["equity"][-1]["cash"] == pytest.approx(cash)
    assert r["metrics"]["total_fees"] == pytest.approx(sum(f["fee"] for f in fills))
    assert r["metrics"]["ending_equity"] == pytest.approx(
        1_000_000 + p["partial_realized_pnl"] + p["unrealized_pnl"]
    )


def test_reduction_intent_cancels_unfilled_additions():
    s, t = fixture([10, 10.7, 11.0, 11.1, 11.0, 10.9, 10.8, 10.7, 10.6, 10.5])
    r = simulate([s], t, "swing_60f_v8", config())
    assert r["trades"][0]["buy_legs"] == 1
    assert r["trades"][0]["entry_notional"] <= 40_000


def test_total_risk_limit_can_prevent_completion_of_third_entry():
    s, t = fixture([10, 10.1, 10.2, 10.6, 10.9, 11.1, 11.2, 11.0, 10.9, 10.8, 10.7])
    cfg = config()
    cfg = replace(cfg, base=replace(cfg.base, risk_budget_pct=0.05))
    r = simulate([s], t, "swing_60f_v8", cfg)
    assert r["trades"][0]["buy_legs"] == 2
    assert r["execution_audit"]["cash_risk_or_lot_rejected"] >= 1


def test_ex_rights_price_changes_do_not_look_like_averaging_down():
    s, t = fixture([10, 10.1, 10.2, 10.6, 10.9, 11.1, 11.2, 11.0, 10.9, 10.8, 10.7])
    s.data["scale"][2:] = 2
    for k in ["open", "high", "low", "close", "up_limit", "down_limit"]:
        s.data[k][2:] /= 2
    r = simulate([s], t, "swing_60f_v8", config())
    trade = r["trades"][0]
    buys = [f for f in trade["fills"] if f["side"] == "buy"]
    assert trade["buy_legs"] == 3 and buys[1]["price"] < buys[0]["price"]
    assert r["metrics"]["ending_equity"] == pytest.approx(1_000_000 + trade["pnl"])
