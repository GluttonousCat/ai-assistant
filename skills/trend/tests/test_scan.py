from datetime import date, datetime
import pandas as pd
import pytest
from jobs import scan_trend


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
