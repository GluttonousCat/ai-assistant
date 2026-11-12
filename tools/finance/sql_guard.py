"""
SQL security validator
Analyzes the sqlglot AST to intercept dangerous operations and ensure the
generated SQL is read-only and safe
"""
from __future__ import annotations

from typing import List

import sqlglot
from sqlglot import exp

# Allowed SQL command types (read-only)
ALLOWED_COMMANDS = {"select", "with", "values", "table", "union", "except", "intersect"}

# Banned functions (attack/abuse risk)
BANNED_FUNCTIONS = {
    "PG_SLEEP", "SLEEP", "BENCHMARK",
    "LOAD_FILE", "OUTFILE", "DUMPFILE",
    "XP_CMDSHELL", "MASTER..XP_CMDSHELL",
}

# Whitelisted schemas allowed for table access
ALLOWED_SCHEMAS = {"stock", "fin"}


class SqlGuardResult:
    """SQL validation result"""

    def __init__(self, valid: bool, error: str = "", warnings: List[str] = None):
        self.valid = valid
        self.error = error
        self.warnings = warnings or []

    def __repr__(self):
        return f"SqlGuardResult(valid={self.valid}, error={self.error!r})"


def validate_sql(sql: str, max_limit: int = 1000) -> SqlGuardResult:
    """
    Validate SQL safety:
    1. Syntax must parse
    2. Must be a read-only query (SELECT/WITH/UNION etc.; INSERT/UPDATE/DELETE/DROP/ALTER banned)
    3. Dangerous functions banned
    4. Only whitelisted schemas may be accessed (stock/fin)
    5. Enforce the LIMIT ceiling
    """
    if not sql or not sql.strip():
        return SqlGuardResult(False, "SQL 为空")

    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
    except Exception as e:
        return SqlGuardResult(False, f"SQL 语法解析失败: {e}")

    # 1. Command type check
    cmd = parsed.key
    if cmd not in ALLOWED_COMMANDS:
        return SqlGuardResult(
            False, f"仅允许只读查询 (SELECT/WITH), 检测到命令类型: {cmd}"
        )

    warnings: List[str] = []

    # 2. Dangerous function check
    for func in parsed.find_all(exp.Func):
        name = func.name.upper()
        if name in BANNED_FUNCTIONS:
            return SqlGuardResult(False, f"检测到禁止函数: {name}")

# 3. Table whitelist check (only checks tables referenced in FROM/JOIN; CTE definitions skipped)
    # Collect CTE names
    cte_names = set()
    for cte in parsed.find_all(exp.CTE):
        name = cte.alias_or_name
        if name:
            cte_names.add(name.lower())

    for table in parsed.find_all(exp.Table):
        table_name = table.name.lower()
        # Skip CTE references
        if table_name in cte_names:
            continue
        # Skip window-function/subquery aliases (e.g. SELECT t.* FROM (sub) t)
        # In sqlglot a subquery alias in FROM is also a Table but has no real schema/table
        has_schema = table.args.get("db")
        if not has_schema:
            if table_name not in _KNOWN_TABLES:
                return SqlGuardResult(
                    False, f"表 {table.name} 不在白名单, 禁止访问"
                )
        else:
            schema_name = str(has_schema).lower()
            if schema_name not in ALLOWED_SCHEMAS:
                return SqlGuardResult(
                    False, f"schema {schema_name} 不在白名单, 禁止访问"
                )

    # 4. LIMIT ceiling check
    limit_expr = parsed.args.get("limit")
    if limit_expr is not None and "expression" in limit_expr.args:
        # Parse the numeric literal
        lit = limit_expr.args["expression"]
        if isinstance(lit, exp.Literal) and lit.is_int:
            val = int(lit.this)
            if val > max_limit:
                warnings.append(f"LIMIT {val} 超过上限 {max_limit}, 将被截断")

    return SqlGuardResult(True, warnings=warnings)


# Whitelisted table set (fallback check when there is no schema prefix)
_KNOWN_TABLES = {
    "stock_basic", "trade_calendar", "daily", "adj_factor",
    "daily_basic", "sync_meta",
    "income", "balancesheet", "cashflow", "fina_indicator", "fina_mainbz",
    "report_meta", "report_forecast", "anomaly_rules", "anomaly_results",
    "query_log",
    # Views
    "v_financial_summary", "v_daily_valuation", "v_main_biz",
}


def force_limit(sql: str, max_limit: int = 1000) -> str:
    """
    Force a LIMIT ceiling onto the SQL (applied when absent or over the cap)
    """
    if not sql or not sql.strip():
        return sql
    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
        limit_expr = parsed.args.get("limit")
        if limit_expr is not None:
            lit = limit_expr.args.get("expression")
            if isinstance(lit, exp.Literal) and lit.is_int:
                val = int(lit.this)
                if val <= max_limit:
                    return sql  # no change needed
        # Override or append the LIMIT
        # Simple approach: attach it at the end (sqlglot round-trip is faithful)
        parsed_limit = sqlglot.parse_one(sql, dialect="postgres")
        parsed_limit.args["limit"] = exp.Limit(expression=exp.Literal.number(max_limit))
        return parsed_limit.sql(dialect="postgres")
    except Exception:
        # On parse failure return the original SQL (validate_sql will report the error)
        return sql