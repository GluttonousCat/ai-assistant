"""Minute-source boundaries: units, filtering, and partial-page failure handling."""
from types import SimpleNamespace
import sys

import pandas as pd
import pytest

from tools.market.minute_history import _boundary, fetch_minute_history, normalize


def test_eastmoney_lots_become_shares_and_date_end_is_inclusive():
    frame = pd.DataFrame({"时间": ["2026-11-11 15:00:00", "2026-11-12 10:00:00"],
                          "开盘": [10, 11], "最高": [11, 12], "最低": [9, 10],
                          "收盘": [10, 11], "成交量": [12, 15], "成交额": [12000, 16500]})
    result = normalize(frame, "akshare-em", "600000.SH", 30, "none",
                       _boundary("2026-11-11"), _boundary("2026-11-11", end=True))
    assert len(result) == 1
    assert result.iloc[0].volume == 1200
    assert result.iloc[0].amount == 12000


def test_baostock_timestamp_and_share_volume():
    frame = pd.DataFrame({"time": ["20261111103000000"], "open": ["10"],
                          "high": ["11"], "low": ["9"], "close": ["10"],
                          "volume": ["12345"], "amount": ["123450"]})
    result = normalize(frame, "baostock", "600000.SH", 60, "none", None, None)
    assert result.iloc[0].bar_end == pd.Timestamp("2026-11-11 10:30:00")
    assert result.iloc[0].volume == 12345


def test_invalid_prices_do_not_silently_turn_into_missing_values():
    frame = pd.DataFrame({"day": ["2026-11-11 10:00:00"], "open": ["bad"],
                          "high": [11], "low": [9], "close": [10], "volume": [12]})
    with pytest.raises(ValueError):
        normalize(frame, "akshare-sina", "600000.SH", 30, "none", None, None)


def test_mid_query_failure_does_not_return_partial_baostock_data(monkeypatch):
    result = SimpleNamespace(error_code="0", error_msg="disconnected", fields=[])

    def next_page():
        result.error_code = "10002007"
        return False

    result.next = next_page
    logged_out = []
    monkeypatch.setitem(sys.modules, "baostock", SimpleNamespace(
        login=lambda: SimpleNamespace(error_code="0"), logout=lambda: logged_out.append(True),
        query_history_k_data_plus=lambda *a, **kw: result))
    with pytest.raises(RuntimeError, match="10002007"):
        fetch_minute_history("baostock", "600000.SH", 30, start="2026-11-11")
    assert logged_out == [True]


def test_sina_cannot_silently_claim_adjusted_data():
    with pytest.raises(ValueError, match="不复权"):
        fetch_minute_history("akshare-sina", "600000.SH", 30, adjustment="forward")
