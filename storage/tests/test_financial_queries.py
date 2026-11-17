from datetime import date
from unittest.mock import MagicMock

import pandas as pd

from storage.repositories import financial as repo


def test_domain_reads_share_read_only_transactions_and_bound_stock_parameters(
    monkeypatch,
):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    pg.fetch_one.return_value = {
        "d": date(2026, 6, 30),
        "industry_l2": "银行Ⅱ",
        "name": "测试",
    }
    pg.fetch_all.return_value = [{"end_date": date(2026, 6, 30)}]
    pg.fetch_df.return_value = pd.DataFrame()
    factory = MagicMock(return_value=pg)
    monkeypatch.setattr(repo, "PgClient", factory)
    functions = [
        (repo.stock_profile_row, ("600000.SH",)),
        (repo.daily_bars_frame, ("600000.SH", 60)),
        (repo.main_business, ("600000.SH", "测试", "P")),
        (repo.valuation_history, ("600000.SH", 3)),
        (repo.dividend_history, ("600000.SH", 5, False)),
        (repo.audit_history, ("600000.SH", 5)),
        (repo.cashflow_history, ("600000.SH", 8)),
        (repo.dupont_history, ("600000.SH", 5)),
        (repo.share_history, ("600000.SH", 5)),
        (repo.risk_inputs, ("600000.SH",)),
        (repo.sell_side_forecasts, ("600000.SH", 365)),
        (repo.top_holders, ("600000.SH", "测试", 1)),
        (repo.industry_members, ("600000.SH", "测试")),
        (repo.industry_financials, ("600000.SH", "测试")),
        (repo.alpha_inputs, ("600000.SH", "测试")),
        (repo.forecast_context, ("600000.SH", "测试")),
    ]
    for function, args in functions:
        function(*args)
    assert factory.call_count == len(functions)
    assert all(call.kwargs == {"read_only": True} for call in factory.call_args_list)
    for call in (
        pg.fetch_all.call_args_list
        + pg.fetch_one.call_args_list
        + pg.fetch_df.call_args_list
    ):
        sql, params = call.args
        assert "600000.SH" not in sql
        assert params
    pg.execute.assert_not_called()


def test_forecast_actuals_use_the_internal_metric_allowlist(monkeypatch):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    pg.fetch_one.return_value = {"v": 100}
    monkeypatch.setattr(repo, "PgClient", lambda **kwargs: pg)
    result = repo.forecast_actuals(
        "600000.SH", {("revenue", 2025): [], ("roe", 2024): []}
    )
    assert set(result) == {("revenue", 2025), ("roe", 2024)}
    assert pg.fetch_one.call_args_list[0].args[1] == ("600000.SH", "2024-12-31")
    assert pg.fetch_one.call_args_list[1].args[1] == ("600000.SH", "2025-12-31")
