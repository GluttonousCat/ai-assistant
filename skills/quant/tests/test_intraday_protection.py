from dataclasses import replace

import pandas as pd
import pytest

from skills.quant.stop_orders import (
    IntradayProtection,
    completed_bar_protection,
    prepare_stop_bars,
)
from skills.quant.tests.test_stop_orders import native
from skills.quant.tests.test_tranches import fixture, config
from skills.quant.tranche_engine import simulate


def test_completed_close_must_meet_both_progress_gates_and_only_raise_the_stop():
    p = dict(stop=9.8, first_price=10, initial_risk=0.2)
    policy = IntradayProtection(0.5, 1.5)
    assert completed_bar_protection(p, 10.12, 10.01, policy) == 9.8
    assert completed_bar_protection(p, 10.2, 10.01, policy) == 10.01
    assert completed_bar_protection({**p, "stop": 10.1}, 10.2, 10.01, policy) == 10.1
    with pytest.raises(ValueError):
        completed_bar_protection(p, float("nan"), 10.01, policy)


def test_new_stop_cannot_fill_against_the_low_of_the_bar_that_raised_it():
    stock, clock = fixture([10, 10, 10.3, 10.3])
    bars = prepare_stop_bars(stock, native(stock))
    day = clock[2].date()
    bars[day] = tuple(
        replace(b, low=9.9 if i == 0 else 10.2, open=10 if i == 0 else 10.3)
        for i, b in enumerate(bars[day])
    )
    result = simulate(
        [stock],
        clock,
        "swing_60f_v8",
        config(),
        stop_bars={stock.code: bars},
        intraday_protection=IntradayProtection(0.5, 1),
    )
    assert result["execution_audit"]["intraday_protection_raises"] >= 1
    assert result["execution_audit"].get("resting_stop_activations", 0) == 0
    assert not result["trades"]


def test_first_day_activation_carries_through_t1_without_cancellation_on_rebound():
    stock, clock = fixture([10, 10.3, 10.4, 10.4])
    result = simulate(
        [stock],
        clock,
        "swing_60f_v8",
        config(),
        stop_bars={stock.code: prepare_stop_bars(stock, native(stock))},
        intraday_protection=IntradayProtection(0.5, 1),
    )
    trade = result["trades"][0]
    assert pd.Timestamp(trade["entry_time"]).date() == clock[1].date()
    assert pd.Timestamp(trade["exit_time"]).date() == clock[2].date()
    assert trade["exit_price"] == 10.3
    assert result["execution_audit"]["t1_exit_deferred"] > 0


def test_intraday_policy_cannot_run_without_explicit_execution_quotes():
    stock, clock = fixture([10, 10.3, 10.4])
    with pytest.raises(ValueError, match="显式"):
        simulate(
            [stock],
            clock,
            "swing_60f_v8",
            config(),
            intraday_protection=IntradayProtection(),
        )


def test_protection_is_invariant_to_an_adjusted_price_basis_change():
    policy = IntradayProtection(0.5, 1)
    p = dict(stop=9.8, first_price=10, initial_risk=0.2)
    factor = 0.987654321
    expected = completed_bar_protection(p, 10.2, 10.01, policy)
    actual = completed_bar_protection(
        {k: v * factor for k, v in p.items()}, 10.2 * factor, 10.01 * factor, policy
    )
    assert actual == pytest.approx(expected * factor)
