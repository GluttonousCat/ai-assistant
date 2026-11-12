"""
SQL template builder
Builds SQL from safe templates based on the (stock, metric, time) slots
extracted by StockKB

Design:
- SELECT-only; all table/column names come from fixed mappings (never user input)
- Supports: single stock with one/multiple metrics (with/without time),
  multi-stock same-metric comparison
- Time handling: annual/quarterly (Q1-Q4)/half-year/last-N-years/latest
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from tools.finance.stock_kb import get_stock_kb

# Tables and joins for the financial/market templates
_TABLE_ALIAS = {
    "i": "fin.income",
    "f": "fin.fina_indicator",
    "b": "fin.balancesheet",
    "c": "fin.cashflow",
    "d": "stock.daily",
    "db": "stock.daily_basic",
}

# Report period -> end_date month-day (financial statement cutoff)
_PERIOD_MD = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}


def _financial_period_filter(time_phrase) -> str:
    """Financial time phrase -> end_date filter clause. Empty = no filter
    (latest first)"""
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
        return ""  # "last year" is relative to today and already resolved to a year by the rules layer; fallback here
    # recent_years / recent / None -> no filter; rely on LIMIT to take the latest
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
    Single-stock query (supports multiple metric columns)
    prefix: 'i'/'f'/'b'/'c' financial tables / 'd'/'db' market tables
    extra_fields: additional metric columns [(field, prefix), ...]
    (financial branch only, at most +2 more)
    """
    if prefix in ("d", "db"):
        # Market data: latest N days (fixed wide-column output)
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

    # Financial: multiple metric columns (primary metric + extras)
    select_parts = [f"{prefix}.{field}"]
    seen = {(prefix, field)}
    for f2, p2 in (extra_fields or [])[:2]:
        if (p2, f2) not in seen:
            select_parts.append(f"{p2}.{f2}")
            seen.add((p2, f2))
    cols = ", ".join(select_parts)

    period_filter = _financial_period_filter(time_phrase)

    # ts_code deliberately not selected: the frontend table has no need for
    # the internal code; the name column already identifies the stock
    return f"""SELECT s.name AS stock_name, i.end_date, {cols}
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
LIMIT 48"""


def build_compare_query(codes: List[Tuple[str, str]], field: str, prefix: str,
                        time_phrase=None) -> Optional[str]:
    """Multi-stock same-metric comparison: codes=[(ts_code, name), ...]"""
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
    # Financial comparison: prefer the specified report period (each stock
    # takes that period); otherwise each stock's latest period
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


_OP_SQL = {
    "gt": ">", "lt": "<", "ge": ">=", "le": "<=",
}


def build_industry_query(
    index_code: str,
    field: str,
    prefix: str,
    time_phrase=None,
    top_n: int = 20,
    level: str = "L2",
    op: Optional[str] = None,
    value: Optional[float] = None,
) -> Optional[str]:
    """
    Query by Shenwan industry: "metric Y of industry X" / "top 5 ROE stocks in
    sector X" / "stocks in sector X with ROE above 20%"
    Joins financial/market tables via stock.v_industry_current
    (stock -> L1/L2 industry)
    op/value: metric condition filter (gt/lt/ge/le + threshold)
    """
    index_col = "index_l2" if level == "L2" else "index_l1"
    period_filter = ""
    if time_phrase:
        kw, val = time_phrase
        if kw == "year":
            period_filter = f" AND i.end_date = '{val}-12-31'"

    if prefix in ("d", "db"):
        # Industry market data: latest trading day
        sql = f"""SELECT s.name AS stock_name,
            COALESCE(i2.industry_l2, i2.industry_l1) AS industry,
            d.trade_date, d.close, d.pct_chg, db.pe_ttm, db.total_mv
FROM stock.v_industry_current i2
JOIN stock.stock_basic s ON i2.ts_code = s.ts_code
JOIN stock.daily d ON i2.ts_code = d.ts_code
LEFT JOIN stock.daily_basic db
    ON d.trade_date = db.trade_date AND d.ts_code = db.ts_code
WHERE i2.{index_col} = '{index_code}'
  AND d.trade_date = (SELECT MAX(trade_date) FROM stock.daily)
ORDER BY d.close DESC
LIMIT {top_n}"""
        return sql

    # Financial industry query: current industry members + the metric's
    # recent report period
    cond = ""
    if op and value is not None:
        cond = f" AND COALESCE({prefix}.{field}, 0) {_OP_SQL[op]} {value}"
    sql = f"""SELECT s.name AS stock_name,
        COALESCE(i2.industry_l2, i2.industry_l1) AS industry,
        i.end_date, {prefix}.{field} AS value
FROM stock.v_industry_current i2
JOIN stock.stock_basic s ON i2.ts_code = s.ts_code
JOIN fin.income i ON i2.ts_code = i.ts_code
LEFT JOIN fin.fina_indicator f
    ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
LEFT JOIN fin.balancesheet b
    ON i.ts_code = b.ts_code AND i.end_date = b.end_date AND i.report_type = b.report_type
WHERE i2.{index_col} = '{index_code}'
  AND i.report_type = '1'{period_filter}{cond}
  AND i.end_date = (SELECT MAX(end_date) FROM fin.income
                     WHERE ts_code = i.ts_code AND report_type='1')
ORDER BY value DESC NULLS LAST
LIMIT {top_n}"""
    return sql


def build_rank_query(
    field: str,
    prefix: str,
    top_n: int = 10,
    op: Optional[str] = None,
    value: Optional[float] = None,
    time_phrase=None,
) -> Optional[str]:
    """
    Whole-market ranking / condition filter: "top-10 ROE across the market" /
    "find stocks with ROE above 20%"
    Each stock uses its latest report period; sort by the metric and take top N
    """
    if prefix in ("d",):
        cond = ""
        if op and value is not None:
            cond = f" AND d.{field} {_OP_SQL[op]} {value}"
        return f"""SELECT s.name AS stock_name, d.trade_date, d.{field} AS value
FROM stock.daily d
JOIN stock.stock_basic s ON d.ts_code = s.ts_code
WHERE d.trade_date = (SELECT MAX(trade_date) FROM stock.daily){cond}
ORDER BY d.{field} DESC NULLS LAST
LIMIT {top_n}"""
    if prefix in ("db",):
        cond = ""
        if op and value is not None:
            cond = f" AND db.{field} {_OP_SQL[op]} {value}"
        return f"""SELECT s.name AS stock_name, db.trade_date, db.{field} AS value
FROM stock.daily_basic db
JOIN stock.stock_basic s ON db.ts_code = s.ts_code
WHERE db.trade_date = (SELECT MAX(trade_date) FROM stock.daily_basic){cond}
ORDER BY db.{field} DESC NULLS LAST
LIMIT {top_n}"""

    # Financial: each stock's latest report period
    period_filter = _financial_period_filter(time_phrase)
    cond = ""
    if op and value is not None:
        cond = f" AND COALESCE(t.value, 0) {_OP_SQL[op]} {value}"
    sql = f"""WITH latest AS (
    SELECT DISTINCT ON (i.ts_code) i.ts_code, i.end_date
    FROM fin.income i WHERE i.report_type = '1'{period_filter}
    ORDER BY i.ts_code, i.end_date DESC
)
SELECT s.name AS stock_name, t.end_date, t.value
FROM (
    SELECT i.ts_code, i.end_date, {prefix}.{field} AS value
    FROM fin.income i
    LEFT JOIN fin.fina_indicator f
        ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
    LEFT JOIN fin.balancesheet b
        ON i.ts_code = b.ts_code AND i.end_date = b.end_date AND i.report_type = b.report_type
    LEFT JOIN fin.cashflow c
        ON i.ts_code = c.ts_code AND i.end_date = c.end_date AND i.report_type = c.report_type
    WHERE i.report_type = '1'
      AND (i.ts_code, i.end_date) IN (SELECT ts_code, end_date FROM latest)
) t
JOIN stock.stock_basic s ON t.ts_code = s.ts_code
WHERE t.value IS NOT NULL{cond}
ORDER BY t.value DESC
LIMIT {top_n}"""
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