"""Read-only market queries used by API/Agent clients; no provider network I/O."""
import re
from storage.repositories.market import read_bars


def query_intraday(code: str, start: str = "2026-01-01", end: str = "2026-12-31",
                   period: int = 60, limit: int = 1000):
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code.upper()):
        raise ValueError("股票代码必须包含SH/SZ/BJ后缀")
    if period not in (5, 15, 30, 60):
        raise ValueError("unsupported minute period")
    from datetime import date
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("开始日期晚于结束日期")
    return read_bars(code.upper(), start, end, period, limit)
