"""Chart SMA must share adjusted prices without changing the strategy EMA."""

from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from skills.trend import service
from skills.trend.strategy import TrendConfig, evaluated_bars
from skills.trend.tests.test_dual_setups import make_setup
from skills.trend.tests.test_strategy import sample
from tools.market.trend_data import adjust_prices


@pytest.mark.parametrize("timeframe", ["60F", "1D"])
@pytest.mark.parametrize("factor", [1.05, 2.0])
def test_dividend_or_split_does_not_create_price_gap_or_change_strategy(
    timeframe, factor, monkeypatch
):
    original = make_setup("pit", timeframe)
    raw = original.copy()
    cut = len(raw) // 2
    prices = ["open", "high", "low", "close"]
    raw.loc[cut:, prices] /= factor
    raw.loc[cut:, "adj_factor"] = factor
    if factor == 2:
        raw.loc[cut:, "volume"] *= 2  # Keep actual traded shares, not price units.
    adjusted, issue = adjust_prices(raw)
    assert issue is None
    np.testing.assert_allclose(adjusted[prices], original[prices] / factor)
    pd.testing.assert_series_equal(adjusted.volume, raw.volume)
    pd.testing.assert_series_equal(adjusted.amount, raw.amount)
    reader = "adjusted_daily_bars" if timeframe == "1D" else "adjusted_bars"
    monkeypatch.setattr(service, reader, lambda *args: raw.copy())
    result = service.stock_detail(
        "000001.SZ",
        "2026-01-01",
        "2026-11-12T15:00:00",
        asdict(TrendConfig()),
        timeframe,
    )
    chart = pd.DataFrame(result["data"])
    expected = evaluated_bars(adjusted, timeframe=timeframe)
    assert result["price_basis"] == "forward_asof"
    assert result["chart_averages"] == {"type": "sma", "windows": [8, 13, 21]}
    for window in [8, 13, 21]:
        np.testing.assert_allclose(
            chart[f"ma{window}"].astype(float),
            expected.close.rolling(window).mean(),
            equal_nan=True,
        )
        assert chart[f"ma{window}"].iloc[: window - 1].isna().all()
    for field in ["eligible", "first_watch", "resume_watch", "startup_count"]:
        assert chart[field].tolist() == expected[field].tolist()
    for key, window in [("ema_fast", 20), ("ema_slow", 60)]:
        np.testing.assert_allclose(
            chart[key].astype(float),
            adjusted.close.ewm(span=window, adjust=False, min_periods=window).mean(),
            equal_nan=True,
        )
    assert chart.ma13.iloc[-1] != pytest.approx(chart.ema_fast.iloc[-1])


def test_chart_averages_use_history_before_display_tail(monkeypatch):
    raw = sample(days=270)
    monkeypatch.setattr(service, "adjusted_bars", lambda *args: raw)
    detail = service.stock_detail(
        "000001.SZ", "2026-01-01", "2027-12-31T15:00:00", asdict(TrendConfig())
    )
    assert len(detail["data"]) == 1000
    first = detail["data"][0]
    for window in [13, 21]:
        assert first[f"ma{window}"] == pytest.approx(
            raw.close.rolling(window).mean().iloc[-1000]
        )


@pytest.mark.parametrize("factor", [float("inf"), 0.0, -1.0])
def test_invalid_adjustment_cannot_produce_watch_or_confirmation(factor, monkeypatch):
    raw = make_setup("pit")
    raw.loc[raw.index[-1], "adj_factor"] = factor
    assert adjust_prices(raw)[1] == "复权因子无效"
    monkeypatch.setattr(service, "adjusted_bars", lambda *args: raw)
    result = service.stock_detail(
        "000001.SZ", "2026-01-01", "2026-11-12T15:00:00", asdict(TrendConfig())
    )
    assert result["issue"] and result["price_basis"] == "raw"
    assert not any(
        row[key]
        for row in result["data"]
        for key in ["eligible", "first_watch", "resume_watch", "swing_ignition"]
    )
