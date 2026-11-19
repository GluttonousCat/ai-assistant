from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from skills.quant.support_protection import (
    SupportProtection,
    attach_support,
    protected_stop,
)
from tests.skills.quant.test_tranches import fixture, config
from skills.quant.tranche_engine import simulate
from tests.skills.trend.test_structure import candles


def sample():
    minute = candles([12, 11, 9, 11, 12, 12, 11, 10, 11, 12, 13, 14, 14, 13, 12, 13])
    stock, _ = fixture([10] * 4)
    return minute, stock


def test_higher_low_requires_two_completed_right_bars_and_daily_prefix_is_invariant():
    minute, stock = sample()
    full = attach_support(minute, stock)
    assert np.isnan(full.data["confirmed_higher_low"][:2]).all()
    assert full.data["confirmed_higher_low"][2] == 9.6
    assert full.data["higher_low_origin"][2] == minute.bar_end.iloc[7].value
    assert full.data["higher_low_known"][2] == minute.bar_end.iloc[9].value
    for size in [1, 2, 3]:
        prefix = replace(
            stock,
            times=stock.times[:size],
            data={k: v[:size] for k, v in stock.data.items()},
        )
        partial = attach_support(minute.iloc[: 4 * size], prefix)
        for key in ["confirmed_higher_low", "higher_low_origin", "higher_low_known"]:
            np.testing.assert_equal(partial.data[key], full.data[key][:size])
    assert "confirmed_higher_low" not in stock.data


def test_missing_native_closing_bar_cannot_reuse_another_days_support():
    minute, stock = sample()
    result = attach_support(minute.drop(index=11), stock)
    assert np.isnan(result.data["confirmed_higher_low"][2])


def test_support_uses_the_frozen_factor_base_without_rewriting_execution_prices():
    minute, stock = sample()
    original = minute.copy(deep=True)
    base = attach_support(minute, stock)
    changed = replace(
        stock, data={**stock.data, "scale": stock.data["scale"] * 0.987654321}
    )
    rebased = attach_support(minute, changed)
    np.testing.assert_allclose(
        rebased.data["confirmed_higher_low"],
        base.data["confirmed_higher_low"] * 0.987654321,
        equal_nan=True,
    )
    pd.testing.assert_frame_equal(minute, original)
    np.testing.assert_equal(changed.data["open"], stock.data["open"])


def test_support_must_form_after_entry_be_known_and_only_raise_a_stop():
    p = dict(stop=9.8, first_price=10, initial_risk=0.2, entry_time="2026-01-06T09:30")
    origin = pd.Timestamp("2026-01-06T14:00").value
    known = pd.Timestamp("2026-01-07T11:30").value
    end = pd.Timestamp("2026-01-07T15:00")
    cfg = SupportProtection(0.5, 0.5)
    assert protected_stop(
        p, 10.2, 0.1, 10.1, origin, known, end, True, cfg
    ) == pytest.approx(10.05)
    assert protected_stop(p, 10.2, 0.1, 9.7, origin, known, end, True, cfg) == 9.8
    assert protected_stop(p, 10.2, 0.1, 10.1, origin, known, end, False, cfg) == 9.8
    assert (
        protected_stop(
            p, 10.2, 0.1, 10.1, origin, known, end - pd.Timedelta(days=1), True, cfg
        )
        == 9.8
    )
    assert (
        protected_stop(
            p, 10.2, 0.1, 10.1, pd.Timestamp("2026-01-05").value, known, end, True, cfg
        )
        == 9.8
    )


def test_ratchet_unlocks_adds_without_expanding_stock_or_account_risk():
    stock, clock = fixture([10, 10.15, 10.25, 10.35, 10.4])
    data = dict(stock.data)
    data["confirmed_higher_low"] = np.array([np.nan, 10.08, 10.18, 10.28, 10.3])
    data["higher_low_origin"] = np.array(
        [(t.normalize() + pd.Timedelta(hours=10, minutes=30)).value for t in clock]
    )
    data["higher_low_known"] = np.array(
        [(t.normalize() + pd.Timedelta(hours=14)).value for t in clock]
    )
    stock = replace(stock, data=data)
    plan = config(entry=(0.5, 0.3, 0.2), exits=(0.5, 0.3, 0.2))
    plan = replace(plan, base=replace(plan.base, max_entry_risk_pct=2.5))
    control = simulate([stock], clock, "swing_60f_v8", plan)
    result = simulate(
        [stock],
        clock,
        "swing_60f_v8",
        plan,
        support_protection=SupportProtection(0.5, 0.25),
    )
    assert control["open_positions"][0]["buy_legs"] == 1
    assert result["open_positions"][0]["buy_legs"] == 3
    fills = result["open_positions"][0]["fills"]
    for fill in fills:
        assert fill["inventory_risk"] <= fill["risk_limit"] + 1e-8
        assert (fill["price"] - fill["stop_adjusted"]) / fill["price"] * 100 <= 2.5
    assert (
        sum(f["notional"] + f["fee"] for f in fills)
        <= plan.base.initial_cash * plan.base.position_weight
    )
