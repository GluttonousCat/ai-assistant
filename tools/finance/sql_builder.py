"""
SQL 模板拼接器
基于 StockKB 提取的 (股票, 指标, 时间) 槽位, 用安全模板拼接 SQL

设计:
- 只拼 SELECT, 所有表名/字段来自限定映射 (非用户输入)
- 支持: 单股单/多指标 (带/不带时间), 多股同指标对比
- 时间处理: 年报/季报(Q1-Q4)/半年报/最近N年/最新
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

# 报告期 -> end_date 月-日 (财报截止日)
_PERIOD_MD = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}


def _financial_period_filter(time_phrase) -> str:
    """财务时间词 -> end_date 过滤子句. 空 = 不过滤 (最新优先)"""
    if not time_phrase:
        return ""
    kw, val = time_phrase
    if kw == "year":
        return f" AND i.end_date = '{val}-12-31'"
    if kw == "quarter":
        y, q = val
        return f" AND i.end_date = '{y}-{_PERIOD_MD[q]}'"
    if kw == "half":
        return f" AND i.end_date = '{val}-06-30'"
    if kw == "previous_year":
        return ""  # 去年需相对今日计算, 规则层已解析为 year; 此处兜底
    # recent_years / recent / None -> 不过滤, 依赖 LIMIT 取最新
    return ""


def build_single_query(
    ts_code: str,
    stock_name: str,
    field: str,
    prefix: str,
    time_phrase=None,
    extra_fields: Optional[List[Tuple[str, str]]] = None,
) -> Optional[str]:
    """
    单股查询 (支持多指标列)
    prefix: 'i'/'f'/'b'/'c' 财务表 / 'd'/'db' 行情表
    extra_fields: 追加指标列 [(field, prefix), ...] (仅财务分支, 最多再 +2)
    """
    if prefix in ("d", "db"):
        # 行情: 最新 N 天 (宽列固定输出)
        cols = (
            "d.trade_date, s.name AS stock_name,"
            " d.close, d.open, d.high, d.low, d.vol, d.amount, d.pct_chg,"
            " db.pe_ttm, db.pb, db.total_mv, db.circ_mv, db.turnover_rate"
        )
        return f"""SELECT {cols}
FROM stock.daily d
JOIN stock.stock_basic s ON d.ts_code = s.ts_code
LEFT JOIN stock.daily_basic db
    ON d.trade_date = db.trade_date AND d.ts_code = db.ts_code
WHERE d.ts_code = '{ts_code}'
ORDER BY d.trade_date DESC
LIMIT 30"""

    # 财务: 多指标列拼接 (首指标 + extras)
    select_parts = [f"{prefix}.{field}"]
    seen = {(prefix, field)}
    for f2, p2 in (extra_fields or [])[:2]:
        if (p2, f2) not in seen:
            select_parts.append(f"{p2}.{f2}")
            seen.add((p2, f2))
    cols = ", ".join(select_parts)

    period_filter = _financial_period_filter(time_phrase)

    return f"""SELECT i.ts_code, s.name AS stock_name, i.end_date, {cols}
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


def build_compare_query(codes: List[Tuple[str, str]], field: str, prefix: str,
                        time_phrase=None) -> Optional[str]:
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
    # 财务对比: 指定报告期优先 (各股取该期), 否则各股最新期
    if time_phrase and time_phrase[0] in ("year", "quarter", "half"):
        period_filter = _financial_period_filter(time_phrase).replace(
            "i.end_date", "t.end_date", 1)
        latest_sub = f"""SELECT ts_code, end_date FROM fin.income
            WHERE ts_code IN ({in_clause}) AND report_type='1'{period_filter}"""
        join_cond = "t.ts_code = i.ts_code AND t.end_date = i.end_date"
    else:
        latest_sub = f"""SELECT DISTINCT ON (ts_code) ts_code, end_date FROM fin.income
            WHERE ts_code IN ({in_clause}) AND report_type='1'
            ORDER BY ts_code, end_date DESC"""
        join_cond = "t.ts_code = i.ts_code AND t.end_date = i.end_date"
    sql = f"""WITH latest AS ({latest_sub})
SELECT s.name AS stock_name, i.end_date, {prefix}.{field} AS value
FROM fin.income i
JOIN latest t ON {join_cond}
JOIN stock.stock_basic s ON i.ts_code = s.ts_code
LEFT JOIN fin.fina_indicator f
    ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
LEFT JOIN fin.balancesheet b
    ON i.ts_code = b.ts_code AND i.end_date = b.end_date AND i.report_type = b.report_type
LEFT JOIN fin.cashflow c
    ON i.ts_code = c.ts_code AND i.end_date = c.end_date AND i.report_type = c.report_type
WHERE i.report_type = '1'
ORDER BY value DESC NULLS LAST
LIMIT 30"""
    return sql


if __name__ == "__main__":
    kb = get_stock_kb()
    kb.load()
    print(build_single_query("600183.SH", "生益科技", "grossprofit_margin", "f", ("year", 2023)))
    print()
    print(build_single_query("000001.SZ", "平安银行", "roe", "f", None,
                             extra_fields=[("revenue", "i")]))
    print()
    print(build_single_query("000001.SZ", "平安银行", "n_cashflow_act", "c",
                             ("quarter", (2023, 1))))
    print()
    print(build_compare_query([("600519.SH", "贵州茅台"), ("000858.SZ", "五粮液")],
                              "grossprofit_margin", "f", ("year", 2023)))