"""Overlap must repair existing dates and include the explicit dashboard pool."""
from datetime import date
from types import SimpleNamespace

import pandas as pd

from tools.market import sync_tushare as sync


def test_overlap_fetches_past_watermark_and_preserves_explicit_new_and_bj_codes(monkeypatch):
    days = [date(2026, 11, 12), date(2026, 11, 13)]
    fetched, written = [], []

    class Pg:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch_one(self, *args):
            return {"m": days[-1]}

    def daily(trade_date):
        fetched.append(trade_date)
        return pd.DataFrame({
            "ts_code": ["001381.SZ", "920001.BJ", "600999.SH"],
            "trade_date": [trade_date] * 3, "close": [1., 2., 3.],
        })

    def flush(frames, *args):
        frame = pd.concat(frames)
        written.extend(frame.to_dict("records"))
        return len(frame)

    monkeypatch.setattr(sync, "PgClient", Pg)
    monkeypatch.setattr(sync, "get_trading_days_in_range", lambda *args: days)
    monkeypatch.setattr(sync, "_flush_batch", flush)
    monkeypatch.setattr(sync, "set_sync_progress", lambda *args: None)
    monkeypatch.setattr(sync.time, "sleep", lambda *args: None)
    pro = SimpleNamespace(daily=daily)
    kwargs = dict(pro=pro, table="daily", api_name="daily", keys=["ts_code", "trade_date"],
                  fields=["ts_code", "trade_date", "close"], start=days[0], end=days[-1],
                  allowed_codes={"001381.SZ", "920001.BJ"})
    assert sync.backfill_table(**kwargs) == 0
    assert fetched == []
    assert sync.backfill_table(**kwargs, resume=False) == 4
    assert fetched == ["20261112", "20261113"]
    assert {r["ts_code"] for r in written} == {"001381.SZ", "920001.BJ"}
