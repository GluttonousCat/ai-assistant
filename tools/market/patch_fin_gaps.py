"""
财务数据精准补齐
1. 补齐整体缺失的股票 (全历史)
2. 补齐 2026 中报缺失的股票 (拉最近公告)

用法: python -m tools.market.patch_fin_gaps
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

import core.net  # noqa: F401  (强制直连)
from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from tools.market.sync_financial import (
    FIN_TABLE_FIELDS,
    _normalize_date_col,
    fetch_stock_fin,
)

logger = get_logger(__name__)


def find_missing_stocks(table: str) -> list:
    """找出某表整体缺失的股票"""
    with PgClient() as pg:
        rows = pg.fetch_all(f"""
            SELECT b.ts_code FROM stock.stock_basic b
            WHERE NOT EXISTS (SELECT 1 FROM fin.{table} f WHERE f.ts_code = b.ts_code)
        """)
    return [r["ts_code"] for r in rows]


def find_missing_period_stocks(table: str, period: str) -> list:
    """找出某表缺指定报告期的股票 (排除整体缺失的)"""
    with PgClient() as pg:
        rows = pg.fetch_all(f"""
            SELECT b.ts_code FROM stock.stock_basic b
            WHERE EXISTS (SELECT 1 FROM fin.{table} f WHERE f.ts_code = b.ts_code)
              AND NOT EXISTS (
                SELECT 1 FROM fin.{table} f2
                WHERE f2.ts_code = b.ts_code AND f2.end_date = %s)
        """, (period,))
    return [r["ts_code"] for r in rows]


def patch_stocks(pro, table: str, codes: list, start: str = "19900101") -> int:
    """补齐指定股票的指定表数据"""
    if not codes:
        logger.info(f"  {table}: 无缺失, 跳过")
        return 0
    api_name, fields = FIN_TABLE_FIELDS[table]
    logger.info(f"  {table}: 补齐 {len(codes)} 只 (start={start})")
    total = 0
    for i, ts_code in enumerate(codes, 1):
        df = fetch_stock_fin(pro, api_name, ts_code, fields, start=start)
        if df is None or df.empty:
            continue
        for c in ("ann_date", "f_ann_date", "end_date"):
            _normalize_date_col(df, c)
        with PgClient() as pg:
            table_cols = {c["name"] for c in pg.get_columns("fin", table)}
        keep = [c for c in df.columns if c in table_cols]
        df = df[keep]
        with PgClient() as pg:
            pg.upsert_df(df, "fin", table,
                         conflict_keys=["ts_code", "end_date", "report_type"])
            total += len(df)
        if i % 100 == 0:
            logger.info(f"    已处理 {i}/{len(codes)}, 累计 {total} 行")
        time.sleep(0.4)
    logger.info(f"  {table}: 补齐完成, {total} 行")
    return total


def main():
    import tushare as ts
    pro = ts.pro_api(get_config().tushare_token)
    period = "2026-06-30"

    print("=== 财务数据精准补齐 ===")
    grand_total = {}

    for table in ["income", "balancesheet", "cashflow", "fina_indicator"]:
        # 1. 整体缺失 -> 全历史
        missing = find_missing_stocks(table)
        n1 = patch_stocks(pro, table, missing, start="19900101")
        # 2. 缺最新报告期 -> 拉最近 90 天公告
        missing_period = find_missing_period_stocks(table, period)
        n2 = patch_stocks(pro, table, missing_period,
                          start=(date.today() - timedelta(days=90)).strftime("%Y%m%d"))
        grand_total[table] = {"full_hist": n1, "period_patch": n2}

    print(f"\n=== 补齐完成: {grand_total} ===")


if __name__ == "__main__":
    main()