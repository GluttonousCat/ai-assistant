import pandas as pd
import pytest

from skills.trend import intraday
from skills.trend.mtf import MtfConfig


def inputs():
    daily = pd.DataFrame({"bar_end": pd.bdate_range(end="2026-11-16", periods=25) + pd.Timedelta(hours=15),
        "open": 20., "high": 21., "low": 19., "close": 20., "volume": 1_000_000.,
        "amount": 50_000_000., "adj_factor": 1.})
    minute = pd.DataFrame({"bar_end": pd.to_datetime(["2026-11-16 15:00", "2026-11-17 10:30", "2026-11-17 11:30"]),
        "open": [20., 10.2, 10.3], "high": [21., 10.6, 10.6], "low": [19., 10.1, 10.2],
        "close": [20., 10.4, 10.5], "volume": 1_000_000., "amount": 50_000_000., "adj_factor": [1., 2., 2.]})
    return minute, daily


def context(frame, cfg):
    f = frame.copy()
    f["reversal_fresh"] = True
    f["reversal_floor"], f["atr"] = 10.2, .2
    f["ma8"], f["ma13"], f["ma21"] = 10.1, 10., 10.2
    f["trend_ready"], f["volume_ratio"] = True, 1.1
    f["reversal_start"] = pd.Timestamp("2026-11-17 10:30")
    f["reversal_age"], f["reversal_bulls"], f["reversal_gain_atr"] = 2, 2, .5
    f["reversal_state"] = "早期反转观察"
    return f


def test_intraday_candidates_never_claim_daily_entry_and_ex_rights_use_common_anchor(monkeypatch):
    minute, daily = inputs()
    seen = []
    monkeypatch.setattr(intraday, "minute_context", context)
    monkeypatch.setattr(intraday, "annotate_summary", lambda row, d, end: seen.append(float(d.close.iloc[-1])) or row)
    result = intraday.observe(minute, daily, "2026-11-17 11:30", "2026-11-16 15:00", {"fresh_reversal": True})
    assert result["status"] == "candidate" and result["eligible"] is False
    assert result["score"] <= 80 and len(result["today_bars"]) == 2
    assert result["new_today"] and 0 < result["risk_pct"] <= MtfConfig().max_risk_pct
    assert seen == [10.] and daily.close.iloc[-1] == 20.


def test_missing_today_factor_is_not_carried_from_yesterday():
    minute, daily = inputs()
    minute.loc[2, "adj_factor"] = None
    result = intraday.observe(minute, daily, "2026-11-17 11:30", "2026-11-16 15:00", {"fresh_reversal": True})
    assert result["status"] == "data_missing" and "复权因子缺失" in result["issue"]


def test_unfinished_daily_candle_cannot_be_used_as_mother_context():
    minute, daily = inputs()
    daily.loc[daily.index[-1], "bar_end"] = pd.Timestamp("2026-11-17 15:00")
    with pytest.raises(ValueError, match="此前已完成"):
        intraday.observe(minute, daily, "2026-11-17 11:30", "2026-11-17 15:00", {"fresh_reversal": True})


def test_an_ordinary_bullish_bar_is_not_promoted_without_the_original_reversal(monkeypatch):
    minute, daily = inputs()
    monkeypatch.setattr(intraday, "minute_context", lambda f, cfg: context(f, cfg).assign(reversal_fresh=False))
    result = intraday.observe(minute, daily, "2026-11-17 11:30", "2026-11-16 15:00", {"fresh_reversal": True})
    assert result["status"] == "filtered"


def test_chart_rejects_a_new_report_instead_of_mixing_snapshot_dates(monkeypatch):
    monkeypatch.setattr(intraday, "intraday_report", lambda: {"run_id": "2026-11-18_1130", "data": []})
    with pytest.raises(LookupError, match="快照已更新"):
        intraday.chart("600000.SH", "2026-11-17_1130")
