import pytest
from psycopg2 import OperationalError, InterfaceError
from jobs.backtest_quant import _database_unit


@pytest.mark.parametrize("error", [OperationalError, InterfaceError])
def test_database_work_unit_retries_transient_disconnect(monkeypatch, error):
    monkeypatch.setattr("jobs.backtest_quant.time.sleep", lambda _: None)
    calls = []

    def action():
        calls.append(1)
        if len(calls) < 3:
            raise error("test disconnect")
        return "completed"

    assert _database_unit(action) == "completed"
    assert len(calls) == 3


def test_data_errors_are_not_hidden_as_database_retries():
    calls = []

    def action():
        calls.append(1)
        raise ValueError("invalid price")

    with pytest.raises(ValueError):
        _database_unit(action)
    assert len(calls) == 1
