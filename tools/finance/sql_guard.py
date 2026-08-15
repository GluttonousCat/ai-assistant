"""
SQL 安全校验器
基于 sqlglot AST 分析, 拦截危险操作, 确保生成的 SQL 只读且安全
"""
from __future__ import annotations

from typing import List, Tuple

import sqlglot
from sqlglot import exp

# 允许的 SQL 命令类型 (只读)
ALLOWED_COMMANDS = {"select", "with", "values", "table", "union", "except", "intersect"}

# 禁止的函数 (存在攻击/滥用风险)
BANNED_FUNCTIONS = {
    "PG_SLEEP", "SLEEP", "BENCHMARK",
    "LOAD_FILE", "OUTFILE", "DUMPFILE",
    "XP_CMDSHELL", "MASTER..XP_CMDSHELL",
}

# 允许访问的表白名单 schema
ALLOWED_SCHEMAS = {"stock", "fin"}


class SqlGuardResult:
    """SQL 校验结果"""

    def __init__(self, valid: bool, error: str = "", warnings: List[str] = None):
        self.valid = valid
        self.error = error
        self.warnings = warnings or []

    def __repr__(self):
        return f"SqlGuardResult(valid={self.valid}, error={self.error!r})"


def validate_sql(sql: str, max_limit: int = 1000) -> SqlGuardResult:
    """
    校验 SQL 安全性:
    1. 语法可解析
    2. 必须是只读查询 (SELECT/WITH/UNION 等, 禁止 INSERT/UPDATE/DELETE/DROP/ALTER)
    3. 禁止危险函数
    4. 只允许访问白名单 schema (stock/fin)
    5. 强制 LIMIT 上限
    """
    if not sql or not sql.strip():
        return SqlGuardResult(False, "SQL 为空")

    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
    except Exception as e:
        return SqlGuardResult(False, f"SQL 语法解析失败: {e}")

    # 1. 命令类型检查
    cmd = parsed.key
    if cmd not in ALLOWED_COMMANDS:
        return SqlGuardResult(
            False, f"仅允许只读查询 (SELECT/WITH), 检测到命令类型: {cmd}"
        )

    warnings: List[str] = []

    # 2. 危险函数检查
    for func in parsed.find_all(exp.Func):
        name = func.name.upper()
        if name in BANNED_FUNCTIONS:
            return SqlGuardResult(False, f"检测到禁止函数: {name}")

# 3. 表白名单检查 (只检查 FROM/JOIN 中引用的表, 跳过 CTE 定义)
    # 收集 CTE 名称
    cte_names = set()
    for cte in parsed.find_all(exp.CTE):
        name = cte.alias_or_name
        if name:
            cte_names.add(name.lower())

    for table in parsed.find_all(exp.Table):
        table_name = table.name.lower()
        # 跳过 CTE 引用
        if table_name in cte_names:
            continue
        # 跳过窗口函数/子查询别名 (如 SELECT t.* FROM (sub) t)
        # sqlglot 中 FROM 里的子查询别名也是 Table 但无实际库表
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

    # 4. LIMIT 上限检查
    limit_expr = parsed.args.get("limit")
    if limit_expr is not None and "expression" in limit_expr.args:
        # 解析数字字面量
        lit = limit_expr.args["expression"]
        if isinstance(lit, exp.Literal) and lit.is_int:
            val = int(lit.this)
            if val > max_limit:
                warnings.append(f"LIMIT {val} 超过上限 {max_limit}, 将被截断")

    return SqlGuardResult(True, warnings=warnings)


# 白名单表集合 (无 schema 前缀时的兜底校验)
_KNOWN_TABLES = {
    "stock_basic", "trade_calendar", "daily", "adj_factor",
    "daily_basic", "sync_meta",
    "income", "balancesheet", "cashflow", "fina_indicator",
    "report_meta", "report_forecast", "anomaly_rules", "anomaly_results",
    "query_log",
    # 视图
    "v_financial_summary", "v_daily_valuation",
}


def force_limit(sql: str, max_limit: int = 1000) -> str:
    """
    为 SQL 强制附加 LIMIT 上限 (若未指定或超限则覆盖)
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
                    return sql  # 无需修改
        # 覆盖或附加 LIMIT
        # 简单方式: 在末尾拼接 (sqlglot 转换保真)
        parsed_limit = sqlglot.parse_one(sql, dialect="postgres")
        parsed_limit.args["limit"] = exp.Limit(expression=exp.Literal.number(max_limit))
        return parsed_limit.sql(dialect="postgres")
    except Exception:
        # 解析失败则返回原 SQL (交由 validate_sql 报错)
        return sql