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


def test_chart_rejects_an_unavailable_archive_instead_of_using_latest(monkeypatch):
    monkeypatch.setattr(intraday, "intraday_report", lambda run_id: None)
    with pytest.raises(LookupError, match="不在已发布"):
        intraday.chart("600000.SH", "2026-11-17_1130")


@pytest.mark.parametrize("timeframe", ["1D", "60F"])
def test_interactive_chart_is_frozen_at_requested_slot_and_uses_true_factor_anchor(monkeypatch, timeframe):
    minute, daily = inputs()
    later = minute.iloc[[-1]].copy()
    later["bar_end"] = pd.Timestamp("2026-11-17 15:00")
    later["close"] = 99.
    minute = pd.concat([minute, later], ignore_index=True)
    daily_later = daily.iloc[[-1]].copy()
    daily_later["bar_end"] = pd.Timestamp("2026-11-17 15:00")
    daily_later["adj_factor"] = 2.
    daily = pd.concat([daily, daily_later], ignore_index=True)
    requested = []
    report = {"run_id": "2026-11-17_1130", "as_of": "2026-11-17 11:30:00", "daily_as_of": "2026-11-16 15:00:00",
              "data": [{"ts_code": "600000.SH", "trend60": {"origin": "2026-11-17T10:30:00"}}]}
    monkeypatch.setattr(intraday, "intraday_report", lambda run: requested.append(run) or report)
    monkeypatch.setattr(intraday, "scan_inputs", lambda *args: minute)
    monkeypatch.setattr(intraday, "scan_daily_inputs", lambda *args: daily)
    result = intraday.chart("600000.SH", report["run_id"], timeframe)
    assert requested == [report["run_id"]]
    assert result["run_id"] == report["run_id"] and result["timeframe"] == timeframe
    assert all(not r["eligible"] for r in result["data"])
    if timeframe == "1D":
        assert result["data"][-1]["close"] == 10.
        assert result["data"][-1]["ma13"] == 10.
        assert result["data"][-1]["bar_end"] == "2026-11-16T15:00:00.000"
        assert result["history_years"] == 5
        assert not any(r["tracking_signal"] for r in result["data"])
    else:
        assert len(result["data"]) == 3 and result["data"][-1]["close"] == 10.5
        assert result["data"][0]["close"] == 10.
        assert sum(r["tracking_signal"] for r in result["data"]) == 1
    assert daily.close.iloc[0] == 20. and minute.close.iloc[0] == 20.


def test_chart_does_not_interpolate_missing_today_factor(monkeypatch):
    minute, _ = inputs()
    minute.loc[2, "adj_factor"] = None
    monkeypatch.setattr(intraday, "intraday_report", lambda run: {"as_of": "2026-11-17 11:30", "daily_as_of": "2026-11-16 15:00", "data": [{"ts_code": "600000.SH"}]})
    monkeypatch.setattr(intraday, "scan_inputs", lambda *args: minute)
    result = intraday.chart("600000.SH", "2026-11-17_1130")
    assert result["data"] == [] and "因子缺失" in result["issue"]


def test_new_watch_is_first_verified_qualification_of_an_existing_structure():
    origin = {"origin": "2026-11-16T11:30:00"}
    rows = [{"ts_code": code, "score": 72., "trend60": origin} for code in "ABCD"]
    yesterday = {"as_of": "2026-11-16 15:00:00", "data": [
        {**rows[0], "score": 71.9}, rows[1]
    ]}
    earlier = {"as_of": "2026-11-17 10:30:00", "data": [rows[0]],
               "scan_status": {"B": "data_missing", "C": "filtered", "D": "data_missing"}}
    intraday.mark_watch_entries(rows, [yesterday, earlier], "2026-11-17 11:30:00")
    assert [(r["new_today"], r["watch_status"], r["watch_since"]) for r in rows] == [
        (True, "newly_qualified", earlier["as_of"]), (False, "continued", yesterday["as_of"]),
        (True, "newly_qualified", "2026-11-17 11:30:00"), (False, "unknown", None)
    ]


def test_watch_time_is_separate_from_origin_and_uses_no_later_round():
    row = {"ts_code": "A", "score": 72., "trend60": {"origin": "2026-11-17T10:30:00"}}
    earlier = {"as_of": "2026-11-17 10:30:00", "data": [{**row, "score": 71.9}]}
    later = {"as_of": "2026-11-17 14:00:00", "data": [row]}
    intraday.mark_watch_entries([row], [earlier, later], "2026-11-17 11:30:00")
    assert row["new_today"] and row["watch_since"] == "2026-11-17 11:30:00"
    assert row["trend60"]["origin"] == "2026-11-17T10:30:00"
    row["score"] = 71.9
    intraday.mark_watch_entries([row], [earlier], "2026-11-17 11:30:00")
    assert not row["new_today"] and row["watch_status"] == "below_threshold"


def test_falling_adjusted_short_average_skips_full_structure_scan(monkeypatch):
    minute, daily = inputs()
    minute = pd.concat([minute.iloc[[0]]] * 9, ignore_index=True)
    minute["bar_end"] = pd.date_range(end="2026-11-17 11:30", periods=9, freq="h")
    minute["close"] = range(20, 11, -1)
    monkeypatch.setattr(intraday, "minute_context", lambda *args: pytest.fail("unnecessary reconstruction"))
    row = intraday.observe(minute, daily, "2026-11-17 11:30", "2026-11-16 15:00", {"fresh_reversal": True})
    assert row["status"] == "filtered" and "短均线" in row["issue"]
