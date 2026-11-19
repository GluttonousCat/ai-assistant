from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from skills.quant.label_research import (
    confirm_absolute_forecasts,
    learn_priorities,
    restore_candidate_features,
)
from skills.quant.online_priority import OnlineConfig
from tests.skills.quant.test_online_priority import record
from tests.skills.quant.test_tranches import fixture


def test_feature_restore_joins_by_code_and_does_not_borrow_cached_forecasts():
    original, _ = fixture([10, 10.2, 10.3])
    cached = replace(
        original,
        data={
            **original.data,
            "risk_atr_pct": np.ones(3),
            "risk_downside20": np.full(3, 2.0),
        },
        signals=[{**original.signals[0], "score": -100}],
    )
    other = replace(original, code="600001.SH")
    other_cached = replace(cached, code=other.code)
    restored = restore_candidate_features([original, other], [other_cached, cached])
    assert restored[0].signals[0]["score"] == 90
    restored[0].data["risk_atr_pct"][0] = 8
    assert cached.data["risk_atr_pct"][0] == 1
    assert "risk_atr_pct" not in original.data


def test_feature_restore_rejects_price_units_and_time_mismatch():
    stock, _ = fixture([10, 10.2, 10.3])
    for mismatched in [
        replace(stock, times=stock.times + pd.Timedelta(days=1)),
        replace(stock, data={**stock.data, "scale": np.full(3, 2)}),
    ]:
        with pytest.raises(ValueError, match="基准不一致"):
            restore_candidate_features([stock], [mismatched])


def test_current_forecast_survives_unknown_future_outcome_and_keeps_native_quality(
    monkeypatch,
):
    stock, _ = fixture([10, 10.2, 10.3])
    clock = pd.bdate_range("2026-02-02", periods=3) + pd.Timedelta(hours=15)
    stock = replace(
        stock,
        times=clock,
        signals=[{**stock.signals[0], "leg": clock[0].isoformat()}],
        data={
            **stock.data,
            "risk_atr_pct": np.ones(3),
            "risk_downside20": np.ones(3),
            "market_context_valid": np.ones(3, dtype=bool),
            "market_systemic_weak": np.zeros(3, dtype=bool),
        },
    )
    records = [
        record("past_a", 0, "2026-01-05", "2026-01-10", 0, 1),
        record("past_b", 0, "2026-01-08", "2026-01-13", 1, 2),
        record(stock.code, 0, clock[0], pd.Timestamp.max, 0.5, np.nan),
    ]
    monkeypatch.setattr(
        "skills.quant.label_research.training_rows", lambda *args: records
    )
    result = learn_priorities([stock], clock, {}, OnlineConfig(min_samples=2))
    signal = result["series"][0].signals[0]
    assert signal["quality_score"] == 90 and signal["priority_score"] > 0
    assert (
        result["audit"]["known_outcomes"] == 2
        and result["audit"]["candidate_rows"] == 3
    )
    assert result["audit"]["first_trained_month"] == "2026-02"
    assert stock.signals[0]["score"] == 90


def test_absolute_confirmation_rejects_relative_winners_that_still_predict_a_loss():
    stock, _ = fixture([10, 10.2, 10.3])
    relative = dict(
        series=[replace(stock, signals=[{**stock.signals[0], "score": 0.5}])],
        models=[],
        model_config={},
    )
    absolute = dict(predictions={(stock.code, 0): -0.1}, models=[])
    result = confirm_absolute_forecasts(relative, [stock], absolute)
    assert not result["series"][0].signals
    absolute["predictions"][(stock.code, 0)] = 0.25
    result = confirm_absolute_forecasts(relative, [stock], absolute, 0.25, 0.5)
    signal = result["series"][0].signals[0]
    assert signal["quality_score"] == 90 and signal["priority_score"] == 0.375
    assert signal["evidence"]["forecast_heads"] == dict(relative=0.5, absolute=0.25)
    assert (
        stock.signals[0]["score"] == 90
        and relative["series"][0].signals[0]["score"] == 0.5
    )
    assert not confirm_absolute_forecasts(
        relative, [stock], dict(predictions={}, models=[])
    )["series"][0].signals
