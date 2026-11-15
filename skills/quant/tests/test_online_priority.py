from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from skills.quant.online_priority import (
    OnlineConfig,
    FEATURES,
    augment_features,
    feature_vector,
    auxiliary_outcome,
    fit_monthly,
    apply_predictions,
)
from skills.quant.tests.test_tranches import fixture
from skills.trend.tests.test_structure import candles


def record(code, i, signal, known, x, y):
    values = np.zeros(len(FEATURES))
    values[0] = x
    return (code, i, pd.Timestamp(signal), values, y, pd.Timestamp(known))


def test_monthly_fit_excludes_unknown_exits_and_future_features_from_scalers():
    rows = [
        record("a", 0, "2026-01-05", "2026-01-10", 0, 1),
        record("a", 1, "2026-01-20", "2026-01-25", 1, 2),
        record("a", 2, "2026-01-31", "2026-02-03", 1e9, 1e9),
        record("a", 3, "2026-02-04", "2026-02-14", 0.5, 1),
        record("a", 4, "2026-02-15", "2026-02-28", 0.6, 2),
        record("a", 5, "2026-03-04", "2026-03-14", 1e10, 1e10),
    ]
    cfg = OnlineConfig(min_samples=2)
    models, full = fit_monthly(rows, cfg)
    prefix_models, prefix = fit_monthly(rows[:4], cfg)
    feb = next(m for m in models if m["month"] == "2026-02")
    assert feb == next(m for m in prefix_models if m["month"] == "2026-02")
    assert feb["samples"] == 2 and feb["feature_means"][0] == 0.5
    assert full[("a", 3)] == prefix[("a", 3)]
    assert all(
        not m["trained"]
        or pd.Timestamp(m["last_label"]) < pd.Period(m["month"]).start_time
        for m in models
    )
    changed = list(rows)
    changed[2] = record("a", 2, "2026-01-31", "2026-02-03", -1e9, -1e9)
    _, other = fit_monthly(changed, cfg)
    assert other[("a", 3)] == full[("a", 3)]


def test_a_current_forecast_does_not_require_a_future_training_label():
    rows = [
        record("a", 0, "2026-01-01", "2026-01-10", 0, 1),
        record("b", 0, "2026-01-02", "2026-01-12", 1, 2),
        record("c", 0, "2026-02-01", pd.Timestamp.max, 0.5, np.nan),
    ]
    models, predictions = fit_monthly(rows, OnlineConfig(min_samples=2))
    assert ("c", 0) in predictions
    assert next(m for m in models if m["month"] == "2026-02")["samples"] == 2


def test_cold_start_stays_in_cash_and_preserves_the_original_candidates():
    stock, _ = fixture([10, 10.1, 10.2])
    stock.data["risk_atr_pct"] = np.ones(3)
    stock.data["risk_downside20"] = np.ones(3)
    assert not apply_predictions([stock], {})[0].signals
    result = apply_predictions([stock], {(stock.code, 0): 0.5})[0]
    assert len(result.signals) == 1 and result.signals[0]["score"] == 0.5
    assert stock.signals[0]["score"] == 90
    models, scores = fit_monthly(
        [
            record("a", 0, "2026-01-01", "2026-01-10", 0, 1),
            record("a", 1, "2026-02-01", "2026-02-10", 1, 2),
        ],
        OnlineConfig(min_samples=2),
    )
    assert not scores and not any(m["trained"] for m in models)


def test_auxiliary_exit_uses_next_open_and_respects_t1_and_limit_deferral():
    stock, times = fixture([10, 9.7, 9.6, 9.5])
    clock = {t: i for i, t in enumerate(times)}
    y, known = auxiliary_outcome(stock, stock.signals[0], clock)
    assert known == times[2] and y < 0
    stock.data["down_limit"][2] = stock.data["open"][2]
    _, known = auxiliary_outcome(stock, stock.signals[0], clock)
    assert known == times[3]


def test_auxiliary_label_cannot_enter_after_missing_next_session_or_use_an_unresolved_exit():
    stock, times = fixture([10, 9.7, 9.6, 9.5])
    clock = {t: i for i, t in enumerate(times)}
    indexes = [0, 2, 3]
    missing = replace(
        stock, times=times[indexes], data={k: v[indexes] for k, v in stock.data.items()}
    )
    y, known = auxiliary_outcome(missing, missing.signals[0], clock)
    assert np.isnan(y) and known == pd.Timestamp.max
    stock.data["down_limit"][2:] = stock.data["open"][2:]
    y, known = auxiliary_outcome(stock, stock.signals[0], clock)
    assert np.isnan(y) and known == pd.Timestamp.max


def test_native_volatility_features_are_prefix_causal_and_price_rebase_invariant():
    prices = 10 + np.sin(np.arange(120) / 7) * 0.1 + np.arange(120) / 100
    raw = candles(prices)
    stock, _ = fixture(prices[3::4])
    for k in (
        "priority_extension",
        "priority_slope",
        "priority_relative",
        "market_excess_pct",
        "market_beta",
    ):
        stock.data[k] = np.ones(len(stock.times))
    for k in ("any_fast", "any_trend", "both_trend", "avoid_both_down", "daily_trend"):
        stock.data["market_mode_" + k] = np.ones(len(stock.times), dtype=bool)
    stock.signals = [{**stock.signals[0], "i": 20, "stop": prices[83] - 0.2}]
    original_raw = stock.data["open"].copy()
    full = augment_features(raw, stock)
    prefix_stock = replace(
        stock, times=stock.times[:24], data={k: v[:24] for k, v in stock.data.items()}
    )
    prefix = augment_features(raw.iloc[:96], prefix_stock)
    np.testing.assert_allclose(
        prefix.data["risk_downside20"],
        full.data["risk_downside20"][:24],
        equal_nan=True,
    )
    data = {k: v.copy() for k, v in stock.data.items()}
    for k in (
        "scale",
        "atr",
        "trail_a",
        "trail_b",
        "adjusted_open",
        "adjusted_high",
        "adjusted_low",
        "adjusted_close",
    ):
        data[k] *= 7
    rebased = augment_features(
        raw,
        replace(
            stock,
            data=data,
            signals=[{**stock.signals[0], "stop": stock.signals[0]["stop"] * 7}],
        ),
    )
    np.testing.assert_allclose(
        feature_vector(full, full.signals[0]),
        feature_vector(rebased, rebased.signals[0]),
        atol=1e-10,
    )
    np.testing.assert_array_equal(stock.data["open"], original_raw)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"penalty": 0},
        {"min_samples": True},
        {"decision_max_risk_pct": 4},
        {"label_slippage": 1},
    ],
)
def test_online_configuration_rejects_invalid_or_looser_risk_parameters(kwargs):
    with pytest.raises(ValueError):
        OnlineConfig(**kwargs)
