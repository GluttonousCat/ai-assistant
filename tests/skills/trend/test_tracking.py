from copy import deepcopy
from datetime import datetime, timezone

import pandas as pd
import pytest

from skills.trend import tracking
from tests.skills.trend.test_digest import row


def bar(day, close=10., factor=1., **fields):
    return {"ts_code": "600000.SH", "bar_end": day + " 15:00:00", "close": close,
            "high": close * 1.02, "low": close * .98, "adj_factor": factor, **fields}


def test_weekends_do_not_count_and_ex_rights_does_not_create_a_false_loss():
    result = tracking.performance([bar("2026-11-13"), bar("2026-11-16", 5.5, 2.)],
                                  "2026-11-13", ["2026-11-13", "2026-11-16"])
    assert result["horizons"]["1"] == {"date": "2026-11-16", "return_pct": 10., "status": "ready"}
    assert result["elapsed_sessions"] == 1 and result["horizons"]["3"]["status"] == "pending"
    assert result["latest_return_pct"] == 10.


def test_suspension_never_shifts_a_horizon_to_the_next_available_bar():
    days = ["2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18"]
    result = tracking.performance([bar(days[0]), bar(days[2], 11.), bar(days[3], 12.)], days[0], days)
    assert result["horizons"]["1"]["status"] == "missing"
    assert result["horizons"]["3"]["return_pct"] == 20.
    assert result["path"][1]["return_pct"] is None
    assert "1个交易日" in result["issue"]


@pytest.mark.parametrize("factor", [None, 0, float("nan"), float("inf")])
def test_invalid_baseline_factor_never_becomes_zero_return(factor):
    result = tracking.performance([bar("2026-11-13", factor=factor), bar("2026-11-16")],
                                  "2026-11-13", ["2026-11-13", "2026-11-16"])
    assert result["latest_return_pct"] is None and result["path"] == []
    assert result["horizons"]["1"]["status"] == "missing"


def test_missing_future_factor_is_not_forward_filled():
    days = ["2026-11-13", "2026-11-16"]
    result = tracking.performance([bar(days[0]), bar(days[1], 20, None)], days[0], days)
    assert result["latest_return_pct"] is None and result["max_up_pct"] is None
    assert result["horizons"]["1"]["status"] == "missing"


def test_signal_day_extremes_are_excluded_and_no_future_data_is_pending():
    first = bar("2026-11-13", high=1000, low=.1)
    result = tracking.performance([first, bar("2026-11-16", high=11, low=9)], "2026-11-13", ["2026-11-13", "2026-11-16"])
    assert result["max_up_pct"] == 10. and result["max_down_pct"] == -10.
    waiting = tracking.performance([first], "2026-11-13", ["2026-11-13"])
    assert waiting["latest_return_pct"] is None
    assert all(v["status"] == "pending" for v in waiting["horizons"].values())


def test_missing_calendar_does_not_invent_trading_day_counts():
    result = tracking.performance([bar("2026-11-13")], "2026-11-13", [])
    assert "交易日历缺失" in result["issue"]
    assert all(v["status"] == "missing" for v in result["horizons"].values())


def test_only_completed_shanghai_daily_bars_are_requested():
    assert tracking.completed_end(datetime(2026, 11, 17, 6, 59, tzinfo=timezone.utc)) == "2026-11-16 15:00:00"
    assert tracking.completed_end(datetime(2026, 11, 17, 7, tzinfo=timezone.utc)) == "2026-11-17 15:00:00"


def test_cohort_uses_frozen_selection_and_defaults_to_a_date_with_forward_time(monkeypatch):
    snapshot = {"run_id": "old", "as_of": "2026-11-13 15:00:00", "version": "old-rules",
                "metadata": {"config": {"fresh_reversal": True}},
                "data": [row("600000.SH"), row("600001.SH", score=59)]}
    original = deepcopy(snapshot)
    monkeypatch.setattr(tracking, "tracking_days", lambda end: [
        {"run_id": "new", "as_of": "2026-11-16 15:00:00"},
        {"run_id": "old", "as_of": snapshot["as_of"]}])
    def cohort(run, *, min_numeric_score):
        assert (run, min_numeric_score) == ("old", 60)
        return snapshot

    monkeypatch.setattr(tracking, "load_dashboard", cohort)
    monkeypatch.setattr(tracking, "tracking_calendar", lambda *a: ["2026-11-13", "2026-11-16"])
    seen = []

    def inputs(codes, start, end):
        seen.append((codes, start, end))
        return pd.DataFrame([bar("2026-11-13"), bar("2026-11-16", 11.)])

    monkeypatch.setattr(tracking, "scan_daily_inputs", inputs)
    result = tracking.follow_up(now=datetime(2026, 11, 17, 10))
    assert result["run_id"] == "old" and result["version"] == "old-rules"
    assert result["summary"]["mean_return_pct"] == 10.
    assert seen == [(["600000.SH"], "2026-11-13", "2026-11-16 15:00:00")]
    assert snapshot == original and not result["data"][0]["eligible"]
    with pytest.raises(LookupError):
        tracking.follow_up("2026-10-01", now=datetime(2026, 11, 17, 10))


def test_chart_keeps_one_original_date_marker_without_new_trade_signals(monkeypatch):
    snapshot = {"run_id": "old", "as_of": "2026-11-13 15:00:00",
                "metadata": {"start": "2026-01-01", "config": {"fresh_reversal": True}}, "data": [row("600000.SH")]}
    def snapshot_for_code(run, *, code):
        assert run == "old"
        return {**snapshot, "data": [r for r in snapshot["data"] if r["ts_code"] == code]}

    monkeypatch.setattr(tracking, "load_dashboard", snapshot_for_code)
    seen = []

    def detail(*args):
        seen.append(args)
        return {"data": [{"bar_end": day + " 15:00:00", "close": price, "eligible": True, "reversal_ignition": True} for day, price in (("2026-11-13", 10.), ("2026-11-16", 12.))]}

    monkeypatch.setattr(tracking, "stock_detail", detail)
    result = tracking.follow_up_chart("600000.SH", "old", now=datetime(2026, 11, 17, 10))
    assert seen[0][2] == "2026-11-16 15:00:00"
    assert [r["tracking_signal"] for r in result["data"]] == [True, False]
    assert result["signal_close"] == 10.
    assert all(not r["eligible"] and not r["reversal_ignition"] for r in result["data"])
    with pytest.raises(LookupError):
        tracking.follow_up_chart("600001.SH", "old")
