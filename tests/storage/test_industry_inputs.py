from unittest.mock import MagicMock

from storage.repositories import industry_data as repo


def test_metric_inputs_remain_batched_and_parameterized(monkeypatch):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    expected = (
        [{"daily": 1}],
        [{"benchmark": 2}],
        [{"valuation": 3}],
        [{"financial": 4}],
    )
    pg.fetch_all.side_effect = expected
    factory = MagicMock(return_value=pg)
    monkeypatch.setattr(repo, "PgClient", factory)
    codes = ["600000.SH", "000001.SZ"]
    assert repo.market_inputs(codes, "000300.SH") == expected
    factory.assert_called_once_with(read_only=True)
    assert [call.args[1] for call in pg.fetch_all.call_args_list] == [
        (codes,),
        ("000300.SH",),
        (codes,),
        (codes,),
    ]
    assert "COALESCE(a.adj_factor, 1)" in pg.fetch_all.call_args_list[0].args[0]


def test_keyword_queries_bind_values_and_empty_keywords_do_not_query(monkeypatch):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    pg.fetch_one.return_value = {"c": 5}
    factory = MagicMock(return_value=pg)
    monkeypatch.setattr(repo, "PgClient", factory)
    assert repo.chain_mentions([]) == 0
    factory.assert_not_called()
    keyword = "x%' OR 1=1 --"
    assert repo.chain_mentions([keyword]) == 5
    sql, params = pg.fetch_one.call_args.args
    assert keyword not in sql and params == [f"%{keyword}%"]
