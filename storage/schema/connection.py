"""Dedicated DDL connection; never borrow a business transaction."""
from contextlib import contextmanager
import psycopg2
from storage.pg import pg_config

@contextmanager
def ddl_cursor():
    """Dedicated standalone connection for DDL (autocommit, closed right after use).

    ALTER/CREATE take table-level locks; inside a business transaction the lock is
    held until commit (whole tables stall during LLM calls), so all ensure-style DDL
    must go through this connection and commit immediately.
    """
    cfg = pg_config()
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], dbname=cfg["database"], connect_timeout=10,
        options="-c search_path=stock,fin,public -c statement_timeout=180000",
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
    try:
        conn.autocommit = True
        yield conn.cursor()
    finally:
        conn.close()


def create_indexes_concurrently(schema, definitions):
    """Deploy module-owned index definitions on a dedicated autocommit connection."""
    with ddl_cursor() as cur:
        cur.execute("SET lock_timeout='5s'")
        cur.execute("SET statement_timeout='10min'")
        for name, definition in definitions.items():
            cur.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")
            qualified_name = f"{schema}.{name}"
            cur.execute("SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid=to_regclass(%s)",
                        (qualified_name,))
            if cur.fetchone() != (True,):
                raise RuntimeError(f"行情索引 {qualified_name} 无效，请检查后重建")
