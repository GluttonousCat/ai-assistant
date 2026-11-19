from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from tools.market import sync_index as module


@pytest.mark.parametrize("issue", [None, "wrong_code", "missing_day", "duplicate", "invalid_close"])
def test_index_write_requires_expected_codes_and_session_coverage(monkeypatch, issue):
    days = (date(2026, 11, 17), date(2026, 11, 18))
    code = "000905.SH"
    bars = pd.DataFrame([
        {"trade_date": day.strftime("%Y%m%d"), "ts_code": code,
         "open": 100, "high": 102, "low": 99, "close": 101,
         "pct_chg": 1, "vol": 10000, "amount": 100000}
        for day in days
    ])
    if issue == "wrong_code":
        bars.loc[0, "ts_code"] = "000300.SH"
    elif issue == "missing_day":
        bars = bars.iloc[1:]
    elif issue == "duplicate":
        bars.loc[0, "trade_date"] = bars.loc[1, "trade_date"]
    elif issue == "invalid_close":
        bars.loc[0, "close"] = -1
    written = []

    def save(frame):
        written.append(frame)
        return len(frame)

    monkeypatch.setattr(module, "save_index_daily", save)
    pro = SimpleNamespace(index_daily=lambda **_: bars)
    if issue:
        with pytest.raises(ValueError):
            module.sync_index(pro, code, *days, required_days=days)
        assert written == []
    else:
        assert module.sync_index(pro, code, *days, required_days=days) == 2
        assert written[0]["trade_date"].tolist() == list(days)
