from unittest.mock import MagicMock

import pandas as pd

from tools.market import sync_financial as sync


def test_incremental_reads_schema_once_and_keeps_per_stock_writes(monkeypatch):
    metadata, first, second = MagicMock(), MagicMock(), MagicMock()
    clients = MagicMock(side_effect=[metadata, first, second])
    monkeypatch.setattr(sync, "PgClient", clients)
    monkeypatch.setattr(sync.time, "sleep", lambda seconds: None)
    columns = ["ts_code", "end_date", "report_type", "created_at"]
    metadata.__enter__.return_value.get_columns.return_value = [{"name": c} for c in columns]
    codes = ["000001.SZ", "000002.SZ"]

    def fetch(pro, api_name, code, fields, start):
        assert fields == columns[:-1] and start == "20260101"
        return pd.DataFrame([dict(ts_code=code, end_date="20251231", report_type="1",
                                  created_at="2026-01-01", unexpected="ignore")])

    monkeypatch.setattr(sync, "fetch_stock_fin", fetch)
    assert sync.fetch_recent_financial(None, "income", codes, "20260101") == 2
    metadata.__enter__.return_value.get_columns.assert_called_once_with("fin", "income")
    for code, client in zip(codes, (first, second)):
        write = client.__enter__.return_value.upsert_df.call_args
        frame, schema, table = write.args
        assert (schema, table) == ("fin", "income") and list(frame) == columns
        assert frame.ts_code.to_list() == [code]
        assert write.kwargs["conflict_keys"] == columns[:3]
        client.__exit__.assert_called_once()
