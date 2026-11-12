"""Single guarded execution path shared by Skills and MCP financial queries."""
import re
from storage.repositories.financial import lookup_stock_code, query_frame
from tools.finance.sql_guard import force_limit, validate_sql


def execute_readonly(sql: str, max_limit: int = 1000):
    # Validate the original statement before any SQL AST round-trip can discard
    # trailing statements. Storage additionally enforces a read-only transaction.
    guard = validate_sql(sql, max_limit=max_limit)
    if not guard.valid:
        raise ValueError(f"SQL 校验失败: {guard.error}")
    bounded = force_limit(sql, max_limit=max_limit)
    return query_frame(bounded)


def lookup_ts_code(name_or_code: str):
    name = name_or_code.strip()
    if not name:
        return None
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", name.upper()):
        return name.upper()
    return lookup_stock_code(name)
