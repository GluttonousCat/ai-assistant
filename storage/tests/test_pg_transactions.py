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


@pytest.mark.parametrize(
    "batches,expected", [([[(1, "a")], [(2, "b")], []], 2), ([[]], 0)]
)
def test_streamed_reads_keep_columns_and_close_cursor_inside_transaction(
    client, batches, expected
):
    pg, connection, _ = client
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.description = [("id",), ("name",)]
    stream.fetchmany.side_effect = batches
    regular = MagicMock()
    connection.cursor.side_effect = (
        lambda *a, **kw: stream if kw.get("name") else regular
    )
    with pg:
        frame = pg.fetch_df_stream(
            "SELECT id,name FROM example WHERE id>%s", (0,), chunk_size=1
        )
        stream.execute.assert_called_once_with(
            "SELECT id,name FROM example WHERE id>%s", (0,)
        )
        assert list(frame.columns) == ["id", "name"] and len(frame) == expected
        stream.__exit__.assert_called_once()
        connection.commit.assert_not_called()
    connection.commit.assert_called_once()


def test_stream_read_failure_returns_a_broken_connection(client):
    pg, connection, returned = client
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.fetchmany.side_effect = psycopg2.OperationalError("wire interrupted")
    connection.cursor.side_effect = (
        lambda *a, **kw: stream if kw.get("name") else MagicMock()
    )
    with pytest.raises(psycopg2.OperationalError):
        with pg:
            pg.fetch_df_stream("SELECT 1")
    stream.__exit__.assert_called_once()
    returned.assert_called_once_with(connection, close=True)


def test_rollback_failure_preserves_original_exception(client):
    pg, connection, returned = client
    with pytest.raises(ValueError, match="original"):
        with pg:
            # Install after the health-check rollback in __enter__.
            connection.rollback.side_effect = psycopg2.OperationalError(
                "rollback disconnected"
            )
            raise ValueError("original")
    returned.assert_called_once_with(connection, close=True)
    connection.commit.assert_not_called()


def test_enter_failure_discards_connection(client):
    pg, connection, returned = client
    # First cursor probes health, the second is the business cursor.
    connection.cursor.side_effect = [
        MagicMock(),
        psycopg2.InterfaceError("cursor failed"),
    ]
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


@pytest.mark.parametrize(
    "row,as_dict,expected",
    [
        ((7, "value"), True, {"id": 7, "name": "value"}),
        ((7, "value"), False, (7, "value")),
        (None, True, None),
    ],
)
def test_fetch_one_does_not_materialize_the_remaining_result(
    client, row, as_dict, expected
):
    pg, connection, _ = client
    cursor = connection.cursor.return_value
    cursor.description = [("id",), ("name",)]
    cursor.fetchone.return_value = row
    cursor.fetchall.side_effect = AssertionError("single row query must not fetch all")
    with pg:
        assert (
            pg.fetch_one("SELECT id,name FROM records WHERE id>%s", (3,), as_dict)
            == expected
        )
    cursor.execute.assert_any_call("SELECT id,name FROM records WHERE id>%s", (3,))
