"""Transaction failures must propagate without leaking a pooled connection."""
from unittest.mock import MagicMock

import psycopg2
import pytest

from storage.pg import PgClient, PgPool


@pytest.fixture
def client(monkeypatch):
    connection = MagicMock()
    connection.closed = False
    monkeypatch.setattr(PgPool, "_pool", MagicMock())
    monkeypatch.setattr(PgPool, "get_conn", lambda: connection)
    returned = MagicMock()
    monkeypatch.setattr(PgPool, "put_conn", returned)
    return PgClient(), connection, returned


def test_failed_commit_is_not_reported_as_success(client):
    pg, connection, returned = client
    connection.commit.side_effect = psycopg2.OperationalError("commit disconnected")
    with pytest.raises(psycopg2.OperationalError, match="commit disconnected"):
        with pg:
            pass
    returned.assert_called_once_with(connection, close=True)


def test_rollback_failure_preserves_original_exception(client):
    pg, connection, returned = client
    with pytest.raises(ValueError, match="original"):
        with pg:
            # Install after the health-check rollback in __enter__.
            connection.rollback.side_effect = psycopg2.OperationalError("rollback disconnected")
            raise ValueError("original")
    returned.assert_called_once_with(connection, close=True)
    connection.commit.assert_not_called()



def test_enter_failure_discards_connection(client):
    pg, connection, returned = client
    # First cursor probes health, the second is the business cursor.
    connection.cursor.side_effect = [MagicMock(), psycopg2.InterfaceError("cursor failed")]
    with pytest.raises(psycopg2.InterfaceError, match="cursor failed"):
        with pg:
            pass
    returned.assert_called_once_with(connection, close=True)


def test_generated_sql_runs_in_read_only_transaction(client):
    pg, connection, _ = client
    pg.read_only = True
    with pg:
        pass
    connection.cursor.return_value.execute.assert_any_call("SET TRANSACTION READ ONLY")
