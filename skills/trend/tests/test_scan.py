from datetime import date, datetime
import pandas as pd
import pytest
from jobs import scan_trend
from skills.trend.tests.test_dual_setups import make_setup


def test_requested_date_cannot_exceed_verified_factor_cutoff(monkeypatch):
    monkeypatch.setattr(
        scan_trend,
        "scan_cutoffs",
        lambda now: {
            "bar_end": datetime(2026, 11, 12, 15),
            "factor_date": date(2026, 11, 11),
        },
    )
    with pytest.raises(ValueError, match="不推定缺失"):
        scan_trend.scan("2026-11-12")


def test_historical_scan_refuses_to_borrow_future_membership(monkeypatch):
    monkeypatch.setattr(
        scan_trend,
        "scan_cutoffs",
        lambda now: {
            "bar_end": datetime(2026, 11, 12, 15),
            "factor_date": date(2026, 11, 12),
        },
    )
    seen = []

    def universe(day):
        seen.append(day)
        return []

    monkeypatch.setattr(scan_trend, "scan_universe", universe)
    with pytest.raises(ValueError, match="未来成分"):
        scan_trend.scan("2026-03-02")
    assert seen == ["2026-03-02"]


def test_whole_pool_data_failure_does_not_publish_a_successful_scan(monkeypatch):
    monkeypatch.setattr(
        scan_trend,
        "scan_cutoffs",
        lambda now: {
            "bar_end": datetime(2026, 11, 12, 15),
            "factor_date": date(2026, 11, 12),
        },
    )
    monkeypatch.setattr(
        scan_trend,
        "scan_universe",
        lambda day: [
            {
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "membership_date": date(2026, 10, 30),
            }
        ],
    )
    monkeypatch.setattr(scan_trend, "scan_inputs", lambda *a: pd.DataFrame())
    with pytest.raises(ValueError, match="未发布"):
        scan_trend.scan()


def test_missing_minute_bars_do_not_suppress_native_daily_opportunity(monkeypatch):
    native = make_setup("pit", "1D").iloc[:-2].copy()
    native["bar_end"] = pd.bdate_range(
        end="2026-11-12", periods=len(native)
    ) + pd.Timedelta(hours=15)
    native["ts_code"] = "600000.SH"
    monkeypatch.setattr(scan_trend, "scan_inputs", lambda *a: pd.DataFrame())
    monkeypatch.setattr(scan_trend, "scan_daily_inputs", lambda *a: native)
    monkeypatch.setattr(
        scan_trend,
        "scan_cutoffs",
        lambda now: {
            "bar_end": datetime(2026, 11, 12, 15),
            "factor_date": date(2026, 11, 12),
            "daily_end": date(2026, 11, 12),
        },
    )
    monkeypatch.setattr(
        scan_trend,
        "scan_universe",
        lambda day: [
            {
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "membership_date": date(2026, 10, 30),
            }
        ],
    )
    _, metadata, rows = scan_trend.scan(workers=1)
    assert rows[0]["state"] == "数据待补"
    assert rows[0]["daily"]["eligible"]
    assert rows[0]["daily"]["pattern"] == "base_rebound"
    assert metadata["timeframes"]["1D"]["eligible"] == 1
