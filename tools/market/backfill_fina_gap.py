"""
fina_indicator 历史缺口补充 (2002-2011 空洞)
只对上市早于 2012 年的老股票重拉 fina_indicator 全量 (upsert 幂等补缺)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient

logger = get_logger(__name__)
API_SLEEP = 1.2


def _to_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s == "nan":
        return None
    if len(s) == 8 and s.isdigit():
        from datetime import datetime
        return datetime.strptime(s, "%Y%m%d").date()
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def get_old_stocks() -> list:
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT ts_code FROM stock.stock_basic "
            "WHERE list_date < '2012-01-01' OR list_date IS NULL"
        )
    return [r["ts_code"] for r in rows]


def backfill_fina_gap(pro):
    from tools.market.sync_financial import FIN_TABLE_FIELDS
    api_name, fields = FIN_TABLE_FIELDS["fina_indicator"]

    codes = get_old_stocks()
    logger.info(f"补充 fina_indicator 缺口: {len(codes)} 只老股")

    total = 0
    done = 0
    for i, ts_code in enumerate(codes, 1):
        try:
            df = getattr(pro, api_name)(ts_code=ts_code)
            time.sleep(API_SLEEP)
        except Exception as e:
            logger.warning(f"  {ts_code} 失败: {str(e)[:60]}")
            time.sleep(3)
            continue

        if df is None or df.empty:
            done += 1
            continue

        for c in ("ann_date", "f_ann_date", "end_date"):
            if c in df.columns:
                df[c] = df[c].map(_to_date)

        with PgClient() as pg:
            cols = {c["name"] for c in pg.get_columns("fin", "fina_indicator")}
            keep = [c for c in df.columns if c in cols]
            df2 = df[keep]
            pg.upsert_df(df2, "fin", "fina_indicator",
                         conflict_keys=["ts_code", "end_date", "report_type"])
            total += len(df2)
        done += 1

        if i % 200 == 0:
            logger.info(f"  已处理 {i}/{len(codes)}, 累计 {total} 行")

    logger.info(f"fina_indicator 缺口补充完成: {total} 行")
    return total


if __name__ == "__main__":
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)
    n = backfill_fina_gap(pro)
    print(f"=== fina_indicator 缺口补充完成: {n} 行 ===")