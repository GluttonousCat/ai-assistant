"""Financial query persistence. SQL validation belongs to the tool boundary."""
import re
from typing import Optional
from storage.pg import PgClient


def query_frame(sql: str):
    with PgClient(read_only=True) as pg:
        return pg.fetch_df(sql)


def lookup_stock_code(name: str) -> Optional[str]:
    with PgClient(read_only=True) as pg:
        row = pg.fetch_one("SELECT ts_code FROM stock.stock_basic WHERE name=%s", (name,))
        if not row:
            row = pg.fetch_one("SELECT ts_code FROM stock.stock_basic WHERE name LIKE %s LIMIT 1", (f"%{name}%",))
        if not row and re.fullmatch(r"\d{6}", name):
            row = pg.fetch_one("SELECT ts_code FROM stock.stock_basic WHERE symbol=%s", (name,))
    return row["ts_code"] if row else None
