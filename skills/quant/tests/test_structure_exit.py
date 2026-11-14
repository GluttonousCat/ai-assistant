import numpy as np
import pytest

from skills.quant.models import StructureExitConfig, BacktestConfig
from skills.quant.structure_exit import decide
from skills.quant.engine import simulate
from skills.quant.tests.test_scheduled_execution import sample


CFG = StructureExitConfig()


def position(family="trend_pullback"):
    return dict(
        entry_adjusted=100.0, initial_risk=2.0, stop=98.0, peak=105.0, family=family
    )


def test_one_r_does_not_prematurely_activate_protection():
    assert decide(position(), 102.0, 1.0, 101.0, True, False, 5, CFG, 100.3) == (
        98.0,
        None,
    )


def test_only_post_entry_confirmed_support_can_raise_the_stop():
    p = position()
    old, _ = decide(p, 105.0, 1.0, 104.0, False, False, 5, CFG, 100.3)
    fresh, _ = decide(p, 105.0, 1.0, 104.0, True, False, 5, CFG, 100.3)
    assert old == 100.3 and fresh == 103.75
    assert fresh < 105.0


def test_small_pullback_and_pending_wave_do_not_force_an_exit():
    stop, reason = decide(position(), 101.0, 1.0, 100.5, True, False, 8, CFG, 100.3)
    assert reason is None and stop == 98.0
    assert (
        decide(position(), 101.0, 1.0, 100.5, True, True, 8, CFG, 100.3)[1]
        == "structure_failed"
    )


def test_rebound_patience_is_not_applied_to_an_orderly_pullback():
    p = position()
    p["peak"] = 100.5
    assert decide(p, 99.0, 1.0, 98.0, True, False, 6, CFG, 100.3)[1] is None
    p["family"] = "base_rebound"
    assert decide(p, 99.0, 1.0, 98.0, True, False, 6, CFG, 100.3)[1] == "rebound_failed"


def test_no_fixed_two_r_target_and_next_day_effective_support():
    s, t, _ = sample()
    s.data["structural_support"] = np.full(len(t), 9.7)
    s.data["support_origin"] = np.full(len(t), t[1].value, dtype=np.int64)
    s.data["structure_failed"] = np.zeros(len(t), dtype=bool)
    # All closes are 10 but intraday highs cross the obsolete fixed target.
    cfg = StructureExitConfig(max_entry_risk_pct=6, slippage=0)
    r = simulate([s], t, "swing_structure_v9", "1D", cfg=cfg)
    assert not r["trades"] and len(r["open_positions"]) == 1
    with pytest.raises(ValueError):
        simulate(
            [s],
            t,
            "swing_structure_v9",
            "1D",
            cfg=BacktestConfig(exit_policy="structure"),
        )
