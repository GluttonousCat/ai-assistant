from jobs.research_quality_60f import CASES, config_for, configuration
from tests.skills.quant.test_tranches import config, fixture
from skills.quant.tranche_engine import simulate


def test_flattened_tranche_report_can_be_replayed_without_changing_accounting():
    stock, clock = fixture([10, 10.1, 10.2, 10.6, 10.9, 11.1, 11.2, 11.0, 10.9, 10.8])
    prior = simulate([stock], clock, "swing_60f_v8", config())
    assert prior["config"]["exit_policy"] == "tranches"
    replay = simulate([stock], clock, "swing_60f_v8", configuration(prior))
    assert replay == prior


def test_declared_cases_never_expand_account_or_actual_entry_risk():
    base = config()
    for name in CASES:
        normal, stressed = config_for(base, name), config_for(base, name, True)
        assert (
            normal.base.risk_budget_pct
            == stressed.base.risk_budget_pct
            == base.base.risk_budget_pct
        )
        assert normal.base.position_weight == base.base.position_weight
        assert normal.base.max_positions == base.base.max_positions
        assert normal.base.max_entry_risk_pct <= base.base.max_entry_risk_pct
        assert normal.base.close_only and stressed.base.close_only
        assert stressed.base.slippage == 2 * normal.base.slippage
