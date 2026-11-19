from contextlib import contextmanager
from unittest.mock import MagicMock

from storage.repositories import chain_extract as repo
from storage.schema import connection, industry


def test_legacy_ensure_signature_does_not_commit_a_business_transaction(monkeypatch):
    cursor, business = MagicMock(), MagicMock()

    @contextmanager
    def ddl():
        yield cursor

    monkeypatch.setattr(connection, "ddl_cursor", ddl)
    repo.init_chain_extract_schema(business)
    cursor.execute.assert_called_once_with(industry.DDL_CHAIN_EXTRACT)
    business.execute.assert_not_called()
    business.conn.commit.assert_not_called()


def test_empty_reextraction_clears_old_segments_in_one_transaction(monkeypatch):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    factory = MagicMock(return_value=pg)
    monkeypatch.setattr(repo, "PgClient", factory)
    repo.replace_segments(7, [])
    factory.assert_called_once_with()
    pg.execute.assert_called_once_with(
        "DELETE FROM fin.chain_extract WHERE report_id=%s", (7,)
    )
    pg.executemany.assert_not_called()
