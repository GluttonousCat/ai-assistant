from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skills.trend.mtf import MtfConfig, evaluate, result
from skills.trend import service


def fixture():
    raw = pd.read_csv(
        Path(__file__).parent / "fixtures/cnnc_20260824.csv", parse_dates=["bar_end"]
    )
    return tuple(
        raw.loc[raw.timeframe.eq(period)].reset_index(drop=True)
        for period in ["60F", "1D"]
    )


def test_cnnc_august_24_is_daily_entry_after_completed_60f_repair():
    m, d = fixture()
    minute, day = evaluate(m, d)
    r = day[day.bar_end.eq("2026-08-24 15:00")].iloc[0]
    assert r.trend_ready and r.quality_candle and r.b_context and r.eligible
    assert r.context_end == r.bar_end
    assert 0 < r.risk_pct <= 5
    assert not day[day.bar_end.between("2026-08-25", "2026-08-29")].eligible.any()
    assert "eligible" not in minute  # Trend states are not executable 60F orders.
    description = result(day.loc[: r.name])
    assert description["timeframe"] == "1D" and description["state"] == "日K入场候选"


def test_daily_candle_alone_cannot_borrow_future_or_incomplete_60f():
    m, d = fixture()
    end = pd.Timestamp("2026-08-24 15:00")
    d = d[d.bar_end <= end]
    for minute in [m[m.bar_end < end], m[~m.bar_end.eq("2026-08-24 11:30")]]:
        _, day = evaluate(minute, d)
        assert day.iloc[-1].quality_candle and not day.iloc[-1].eligible
        assert not day.iloc[-1].context_complete


def test_later_bars_do_not_rewrite_trend_or_entry():
    m, d = fixture()
    full_m, full_d = evaluate(m, d)
    cols = [
        "eligible",
        "trend_ready",
        "quality_candle",
        "stop_price",
        "context_end",
        "score",
    ]
    for end in [
        "2026-08-21 15:00",
        "2026-08-24 15:00",
        "2026-08-25 15:00",
        "2026-09-15 15:00",
        "2026-11-12 15:00",
    ]:
        a, b = evaluate(m[m.bar_end <= end], d[d.bar_end <= end])
        pd.testing.assert_frame_equal(b[cols], full_d.loc[full_d.bar_end <= end, cols])
        pd.testing.assert_series_equal(
            a.trend_ready, full_m.loc[full_m.bar_end <= end, "trend_ready"]
        )


@pytest.mark.parametrize("scale", [0.987654321, 1.23456789, 3.1415926535])
def test_factor_rebasing_cannot_flip_flat_ma_or_entry(scale):
    m, d = fixture()
    before_m, before_d = evaluate(m, d)
    for f in [m, d]:
        f[["open", "high", "low", "close"]] *= scale
    after_m, after_d = evaluate(m, d)
    pd.testing.assert_series_equal(before_m.trend_ready, after_m.trend_ready)
    pd.testing.assert_series_equal(before_d.eligible, after_d.eligible)


def test_a_flat_daily_candle_does_not_trade_on_minute_trend_alone():
    m, d = fixture()
    i = d.index[d.bar_end.eq("2026-08-24 15:00")][0]
    d.loc[i, "open"] = d.loc[i, "close"]
    _, day = evaluate(m, d)
    assert (
        day.iloc[i].trend_ready
        and not day.iloc[i].quality_candle
        and not day.iloc[i].eligible
    )


@pytest.mark.parametrize("period", ["60F", "1D"])
def test_chart_entry_markers_exist_only_on_daily_and_averages_are_m81321(
    period, monkeypatch
):
    m, d = fixture()
    monkeypatch.setattr(service, "adjusted_bars", lambda *args: m.assign(adj_factor=1))
    monkeypatch.setattr(
        service, "adjusted_daily_bars", lambda *args: d.assign(adj_factor=1)
    )
    chart = service.stock_detail(
        "601985.SH", "2026-01-01", "2026-11-12T15:00:00", asdict(MtfConfig()), period
    )
    assert chart["chart_averages"]["windows"] == [8, 13, 21]
    assert chart["entry_timeframe"] == "1D" and chart["trend_timeframe"] == "60F"
    if period == "60F":
        assert not any(r["eligible"] for r in chart["data"])
    else:
        assert next(r for r in chart["data"] if r["bar_end"].startswith("2026-08-24"))[
            "eligible"
        ]
    assert not any(r["first_watch"] for r in chart["data"])


@pytest.mark.parametrize(
    "change",
    [{"fast": 13}, {"stop_atr": 0}, {"cooldown_days": 0}, {"max_risk_pct": np.nan}],
)
def test_invalid_mtf_parameters_are_rejected(change):
    with pytest.raises(ValueError):
        MtfConfig(**change)
