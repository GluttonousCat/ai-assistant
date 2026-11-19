from dataclasses import replace

import numpy as np
import pytest

from tests.skills.quant.test_tranches import fixture
from skills.quant.wyckoff_hybrid import filter_pullbacks
from tests.skills.trend.test_structure import candles


def test_hybrid_gates_only_lps_and_uses_the_confirmation_bar():
    stock, clock = fixture([10] * 10)
    minute = candles(np.linspace(9, 11, 40))
    trigger = minute.bar_end.iloc[29]
    pullback = {
        **stock.signals[0],
        "i": 7,
        "evidence": {"event": "lps", "native_event_time": trigger.isoformat()},
    }
    reversal = {
        **pullback,
        "family": "base_rebound",
        "i": 6,
        "evidence": {"event": "spring_test"},
    }
    stock = replace(stock, signals=[reversal, pullback])
    result = filter_pullbacks(minute, stock)
    assert len(result.signals) == 2
    assert result.signals[0] == reversal
    context = result.signals[1]["evidence"]["trend_context"]
    assert context["at"] == trigger.isoformat()
    assert context["m13"] > context["m21"]
    assert filter_pullbacks(minute.iloc[:30], stock).signals == result.signals
    assert filter_pullbacks(candles(np.linspace(11, 9, 40)), stock).signals == [
        reversal
    ]
    with pytest.raises(ValueError, match="确认时点"):
        filter_pullbacks(minute.drop(index=29), stock)
