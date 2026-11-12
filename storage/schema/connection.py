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
