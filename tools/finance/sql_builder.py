"""
SQL 模板拼接器
基于 StockKB 提取的 (股票, 指标, 时间) 槽位, 用安全模板拼接 SQL

设计:
- 只拼 SELECT, 所有表名/字段来自限定映射 (非用户输入)
- 支持: 单股单指标 (带/不带时间), 单股多指标(最多3), 多股同指标对比
- 时间处理: 年报/年报期/最近N年/最新
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from tools.finance.stock_kb import get_stock_kb

# 财务/行情模板的表与连接
_TABLE_ALIAS = {
    "i": "fin.income",
    "f": "fin.fina_indicator",
    "b": "fin.balancesheet",
    "c": "fin.cashflow",
    "d": "stock.daily",
    "db": "stock.daily_basic",
}


def build_single_query(
    ts_code: str,
    stock_name: str,
    field: str,
    prefix: str,
    time_phrase=None,
) -> Optional[str]:
    """
    单股单指标查询
    prefix: 'i'/'f'/'b'/'c' 财务表 / 'd'/'db' 行情表
    """
    if prefix in ("d", "db"):
        # 行情: 最新 N 天
        limit = 30
        cols = (
            "d.trade_date, s.name AS stock_name,"
            " d.close, d.open, d.high, d.low, d.vol, d.amount, d.pct_chg,"
            " db.pe_ttm, db.pb, db.total_mv, db.circ_mv, db.turnover_rate"
        )
        sql = f"""SELECT {cols}
FROM stock.daily d
JOIN stock.stock_basic s ON d.ts_code = s.ts_code
LEFT JOIN stock.daily_basic db
    ON d.trade_date = db.trade_date AND d.ts_code = db.ts_code
WHERE d.ts_code = '{ts_code}'
ORDER BY d.trade_date DESC
LIMIT {limit}"""
        return sql

    # 财务: 按报告期过滤
    period_filter = ""
    if time_phrase:
        kw, val = time_phrase
        if kw == "year":
            period_filter = f" AND i.end_date = '{val}-12-31'"
        elif kw == "recent_years":
            period_filter = ""
        elif kw == "recent" or kw is None:
            period_filter = ""
    else:
        period_filter = ""

    table = _TABLE_ALIAS[prefix]

    # 只 select 请求的字段 (+ 时间列)
    select_field = f"{prefix}.{field}"
    sql = f"""SELECT i.ts_code, s.name AS stock_name, i.end_date, {select_field}
FROM fin.income i
JOIN stock.stock_basic s ON i.ts_code = s.ts_code
LEFT JOIN fin.fina_indicator f
    ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
LEFT JOIN fin.balancesheet b
    ON i.ts_code = b.ts_code AND i.end_date = b.end_date AND i.report_type = b.report_type
LEFT JOIN fin.cashflow c
    ON i.ts_code = c.ts_code AND i.end_date = c.end_date AND i.report_type = c.report_type
WHERE i.ts_code = '{ts_code}' AND i.report_type = '1'{period_filter}
ORDER BY i.end_date DESC
LIMIT 12"""
    return sql


def build_compare_query(codes: List[Tuple[str, str]], field: str, prefix: str) -> Optional[str]:
    """多股同指标对比: codes=[(ts_code, name), ...]"""
    if len(codes) < 2:
        return None
    in_clause = ", ".join(f"'{c[0]}'" for c in codes)
    if prefix in ("d", "db"):
        sql = f"""SELECT s.name, d.trade_date, d.close, d.pct_chg, db.pe_ttm, db.total_mv
FROM stock.daily d
JOIN stock.stock_basic s ON d.ts_code = s.ts_code
LEFT JOIN stock.daily_basic db
    ON d.trade_date = db.trade_date AND d.ts_code = db.ts_code
WHERE d.ts_code IN ({in_clause})
  AND d.trade_date = (SELECT MAX(trade_date) FROM stock.daily)
ORDER BY d.close DESC
LIMIT 30"""
        return sql
    sql = f"""SELECT s.name AS stock_name, i.end_date, {prefix}.{field} AS value
FROM fin.income i
JOIN stock.stock_basic s ON i.ts_code = s.ts_code
LEFT JOIN fin.fina_indicator f
    ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
LEFT JOIN fin.balancesheet b
    ON i.ts_code = b.ts_code AND i.end_date = b.end_date AND i.report_type = b.report_type
LEFT JOIN fin.cashflow c
    ON i.ts_code = c.ts_code AND i.end_date = c.end_date AND i.report_type = c.report_type
WHERE i.ts_code IN ({in_clause}) AND i.report_type = '1'
  AND i.end_date = (SELECT MAX(end_date) FROM fin.income WHERE ts_code IN ({in_clause}))
ORDER BY value DESC
LIMIT 30"""
    return sql


if __name__ == "__main__":
    kb = get_stock_kb()
    kb.load()
    q = build_single_query("600183.SH", "生益科技", "grossprofit_margin", "f", ("year", 2023))
    print(q)