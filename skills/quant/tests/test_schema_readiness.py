from contextlib import contextmanager

from storage.schema import quant


def cursor(monkeypatch, objects):
    calls = []

    class Cur:
        def execute(self, sql):
            calls.append(sql)

        def fetchone(self):
            return objects

    @contextmanager
    def fake():
        yield Cur()

    monkeypatch.setattr(quant, "ddl_cursor", fake)
    return calls


def test_ready_schema_never_relocks_tables_with_create_index_or_comments(monkeypatch):
    calls = cursor(monkeypatch, ("limit", "run", "trade", "index"))
    quant.ensure_quant_schema()
    assert len(calls) == 2
    assert not any("CREATE " in sql or "COMMENT " in sql for sql in calls)


def test_missing_schema_object_still_deploys_ddl_on_independent_connection(monkeypatch):
    calls = cursor(monkeypatch, ("limit", "run", "trade", None))
    quant.ensure_quant_schema()
    assert "CREATE INDEX IF NOT EXISTS" in calls[-1]
