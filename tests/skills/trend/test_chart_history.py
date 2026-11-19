import pandas as pd

from skills.trend import chart_history as chart


def history():
    dates = pd.bdate_range("2021-09-01", "2026-11-16") + pd.Timedelta(hours=15)
    return pd.DataFrame({"bar_end": dates, "open": 10., "high": 11., "low": 9.,
                         "close": 10., "volume": 100., "amount": 1000., "adj_factor": 1.})


def test_five_year_prices_do_not_change_the_frozen_signal_window_or_truncate_at_1000(monkeypatch):
    calls = []
    frozen = {"data": [{"bar_end": "2026-11-13T15:00:00", "close": 10., "eligible": True}], "price_basis": "forward_asof", "issue": None}
    monkeypatch.setattr(chart, "stock_detail", lambda *a: calls.append(a) or frozen)
    monkeypatch.setattr(chart, "adjusted_daily_bars", lambda *a: history())
    result = chart.stock_chart("600000.SH", "2026-01-01", "2026-11-16 15:00:00", {}, "1D")
    assert calls[0][1] == "2026-01-01"
    assert len(result["data"]) > 1100
    assert result["data"][0]["bar_end"][:10] == "2021-11-16"
    assert result["data"][0]["ma21"] == 10.
    signals = [r for r in result["data"] if r["eligible"]]
    assert len(signals) == 1 and signals[0]["bar_end"][:10] == "2026-11-13"
    assert len(frozen["data"]) == 1


def test_missing_historical_factors_never_produce_a_fake_unadjusted_five_year_chart(monkeypatch):
    frame = history()
    frame.loc[20, "adj_factor"] = None
    monkeypatch.setattr(chart, "stock_detail", lambda *a: {"data": [], "issue": None})
    monkeypatch.setattr(chart, "adjusted_daily_bars", lambda *a: frame)
    result = chart.stock_chart("600000.SH", "2026-01-01", "2026-11-16", {}, "1D")
    assert "复权因子缺失" in result["issue"] and result["data"] == []


def test_minute_chart_and_leap_year_range_keep_their_own_contract(monkeypatch):
    original = {"data": []}
    monkeypatch.setattr(chart, "stock_detail", lambda *a: original)
    assert chart.stock_chart("600000.SH", "2026-01-01", "2026-11-16", {}, "60F") is original
    assert str(chart.five_year_start("2024-02-29")) == "2019-02-28"
