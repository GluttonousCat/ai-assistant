from dataclasses import replace

import pandas as pd
import pytest

from skills.quant.candidate_quality import attach_signal_quality
from skills.quant.tests.test_tranches import fixture


def pair(quality=72, priority=0.3):
    stock, _ = fixture([10, 10.2, 10.3])
    original = replace(stock, signals=[{**stock.signals[0], "score": quality}])
    ranked = replace(stock, signals=[{**stock.signals[0], "score": priority}])
    return ranked, original


def test_quality_gate_does_not_filter_on_prediction_or_mutate_frozen_inputs():
    ranked, original = pair()
    selected = attach_signal_quality([ranked], [original])[0].signals[0]
    assert selected["quality_score"] == 72
    assert selected["priority_score"] == selected["score"] == 0.3
    assert selected["evidence"]["selection"]["quality_scale"] == "v8-native-0-120"
    assert (
        ranked.signals[0]["score"] == 0.3 and "quality_score" not in ranked.signals[0]
    )
    assert original.signals[0]["score"] == 72
    ranked, original = pair(71.999, 100)
    assert not attach_signal_quality([ranked], [original])[0].signals


def test_source_join_uses_codes_and_signal_identity_even_when_reordered():
    a, raw_a = pair(80, 0.1)
    b, raw_b = pair(90, 0.9)
    b, raw_b = replace(b, code="600001.SH"), replace(raw_b, code="600001.SH")
    selected = attach_signal_quality([a, b], [raw_b, raw_a])
    assert [s.signals[0]["quality_score"] for s in selected] == [80, 90]
    with pytest.raises(ValueError, match="股票池"):
        attach_signal_quality([a, a], [raw_a, raw_b])


@pytest.mark.parametrize(
    "field,value", [("leg", "unknown"), ("stop", 98), ("family", "other")]
)
def test_mismatched_trigger_or_price_units_fail_closed(field, value):
    ranked, original = pair()
    ranked.signals = [{**ranked.signals[0], field: value}]
    with pytest.raises(ValueError):
        attach_signal_quality([ranked], [original])


def test_mismatched_clock_and_nonfinite_scores_are_rejected():
    ranked, original = pair()
    with pytest.raises(ValueError, match="时间轴"):
        attach_signal_quality(
            [replace(ranked, times=ranked.times + pd.Timedelta(days=1))], [original]
        )
    for quality, priority in [(float("nan"), 1), (90, float("inf")), (121, 1)]:
        ranked, original = pair(quality, priority)
        with pytest.raises(ValueError, match="分无效"):
            attach_signal_quality([ranked], [original])
