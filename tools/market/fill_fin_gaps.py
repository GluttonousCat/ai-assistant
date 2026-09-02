"""
财务数据精准补缺 (一次性脚本)
找出报告期缺失的股票 (对比应有期数 vs 实际期数), 只对缺失股票全量重拉
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from core.net import force_direct_connection  # noqa: F401
from storage.pg import PgClient

logger = get_logger(__name__)

API_SLEEP = 0.5  # 直连快, 200次/分钟留余量


def find_missing_stocks(table: str = "income") -> list:
    """找出报告期缺失的股票 (实际 < 应有 - 1)"""
    with PgClient() as pg:
        rows = pg.fetch_all(f"""
            SELECT b.ts_code, b.name, b.list_date,
                   COUNT(DISTINCT t.end_date) actual
            FROM stock.stock_basic b
            JOIN fin.{table} t ON b.ts_code = t.ts_code
            WHERE t.end_date >= '2020-01-01'
            GROUP BY b.ts_code, b.name, b.list_date
            HAVING COUNT(DISTINCT t.end_date) < 20""")

    def expected_periods(list_date):
        if list_date is None:
            return 27
        start = max(list_date, date(2020, 1, 1))
        end = date(2026, 6, 30)
        if start > end:
            return 0
        count = 0
        for y in range(start.year, end.year + 1):
            for md in [(3, 31), (6, 30), (9, 30), (12, 31)]:
                d = date(y, *md)
                if start <= d <= end:
                    count += 1
        return count

    missing = []
    for r in rows:
        exp = expected_periods(r["list_date"])
        if r["actual"] < exp - 1:
            missing.append(r["ts_code"])
    return missing


def backfill_stocks(pro, table: str, codes: list,
                    fields: list) -> int:
    """对指定股票全量重拉 (19900101 起)"""
    from tools.market.sync_financial import fetch_stock_fin, _normalize_date_col

    total = 0
    for i, ts_code in enumerate(codes, 1):
        df = fetch_stock_fin(pro, table, ts_code, fields)
        if df is None or df.empty:
            logger.warning(f"  {table} {ts_code} 无数据")
            continue
        for c in ("ann_date", "f_ann_date", "end_date"):
            _normalize_date_col(df, c)
        with PgClient() as pg:
            table_cols = {c["name"] for c in pg.get_columns("fin", table)}
            keep = [c for c in df.columns if c in table_cols]
            df = df[keep]
            pg.upsert_df(df, "fin", table,
                         conflict_keys=["ts_code", "end_date", "report_type"])
            total += len(df)
        if i % 50 == 0:
            logger.info(f"  {table}: 已补 {i}/{len(codes)}, 累计 {total} 行")
        time.sleep(API_SLEEP)
    return total


def main():
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)

    # 四表的字段映射
    from tools.market.sync_financial import FIN_TABLE_FIELDS

    grand_total = {}
    for table in ["income", "balancesheet", "cashflow", "fina_indicator"]:
        missing = find_missing_stocks(table)
        logger.info(f"{table}: 发现缺失 {len(missing)} 只")
        if not missing:
            continue
        api_name, fields = FIN_TABLE_FIELDS[table]
        n = backfill_stocks(pro, api_name, missing, fields)
        grand_total[table] = n
        logger.info(f"{table}: 补齐完成, 写入 {n} 行")

    print(f"=== 补缺完成: {grand_total} ===")


if __name__ == "__main__":
    main()