import pytest

from storage.repositories import backtest


def test_nested_fills_can_use_small_batches_without_changing_trade_order(monkeypatch):
    calls = []

    class Pg:
        cur = object()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args):
            calls.append("run")

    def values(cursor, sql, rows, page_size):
        calls.append((page_size, [row[1] for row in rows]))

    monkeypatch.setattr(backtest, "PgClient", Pg)
    monkeypatch.setattr(backtest, "execute_values", values)
    r = dict(
        run_id="run",
        suite_id="suite",
        strategy_id="test",
        timeframe="1D",
        start="2026-01-01",
        end="2026-11-12",
        universe_mode="fixed_snapshot",
        trades=[{"pnl": 1}, {"pnl": 2}],
    )
    backtest.publish(r)
    backtest.publish(r, trade_page_size=5)
    assert calls == ["run", (500, [1, 2]), "run", (5, [1, 2])]


def test_invalid_transport_batch_is_rejected_before_any_connection(monkeypatch):
    monkeypatch.setattr(
        backtest, "PgClient", lambda: pytest.fail("unexpected database connection")
    )
    for size in [0, 501, False, 1.5]:
        with pytest.raises(ValueError):
            backtest.publish({}, trade_page_size=size)
