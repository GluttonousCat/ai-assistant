from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from skills.quant.forward_replay import (
    append_evidence,
    extend_execution,
    frozen_predictions,
)
from skills.quant.online_priority import FEATURES
from tests.skills.quant.test_tranches import fixture


def test_extension_keeps_old_units_and_appends_only_causal_rows():
    stock, clock = fixture([10, 11, 12])
    original = replace(
        stock, times=clock[:2], data={k: v[:2] for k, v in stock.data.items()}
    )
    new = pd.DataFrame(
        {
            k: [stock.data[k][2]]
            for k in (
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "up_limit",
                "down_limit",
            )
        }
    )
    new["bar_end"], new["adj_factor"] = clock[2], 3.0
    anchor = SimpleNamespace(bar_end=clock[1], adj_factor=2.0, factor_base=2.0)
    execution = extend_execution(original, new, anchor)
    np.testing.assert_array_equal(execution.data["close"], [10, 11, 12])
    np.testing.assert_array_equal(execution.data["adjusted_close"], [10, 11, 18])
    computed = replace(
        execution,
        data={**stock.data, **execution.data},
        signals=[
            {**stock.signals[0], "score": -100},
            {**stock.signals[0], "i": 2},
        ],
    )
    result = append_evidence(original, computed)
    assert result.signals[0] == original.signals[0]
    assert result.signals[1]["i"] == 2
    np.testing.assert_array_equal(result.data["adjusted_close"], [10, 11, 18])
    with pytest.raises(ValueError, match="复权基准"):
        extend_execution(
            original,
            new,
            SimpleNamespace(bar_end=clock[1], adj_factor=3, factor_base=2),
        )


def test_frozen_model_obeys_scaling_and_label_cutoff(monkeypatch):
    stock, clock = fixture([10, 11, 12])
    model = dict(
        month="2026-01",
        trained=True,
        last_label="2025-12-31",
        coefficients={k: 1 for k in FEATURES},
        feature_means=[1] * len(FEATURES),
        feature_stds=[2] * len(FEATURES),
        intercept=0.25,
    )
    monkeypatch.setattr(
        "skills.quant.forward_replay.feature_vector",
        lambda *args: np.full(len(FEATURES), 3.0),
    )
    assert frozen_predictions(stock, model)[stock.code, 0] == len(FEATURES) + 0.25
    with pytest.raises(ValueError, match="未到期"):
        frozen_predictions(stock, {**model, "last_label": clock[0]})
