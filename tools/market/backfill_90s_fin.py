"""
财务 90 年代数据补充 (一次性脚本)
覆盖: income / balancesheet / fina_indicator (cashflow 90s 无数据, 跳过)
原理: 遍历白名单股票, 拉 1990-1999 期间数据, upsert 入库 (幂等)
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

API_SLEEP = 1.2  # 200次/分钟 -> 保险 1.2s

# 要补充的表: (接口名, 主键, 需要的列由接口全字段返回时取表已有)
TABLES = ["income", "balancesheet", "fina_indicator"]


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


def get_old_stock_codes() -> list:
    """获取 2000 年前上市的白名单股票 (90s 有数据的老股)"""
    from tools.market.sync_financial import get_whitelist_codes
    codes = get_whitelist_codes()
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all("SELECT ts_code FROM stock.stock_basic WHERE list_date < '2000-01-01'")
        old = {r["ts_code"] for r in rows}
    result = [c for c in codes if c in old]
    logger.info(f"2000 年前上市股票: {len(result)} 只")
    return result


def backfill_90s(pro, retry: int = 2) -> dict:
    codes = get_old_stock_codes()
    logger.info(f"补充 90s 财务: {len(codes)} 只股票, 表 {TABLES}")

    result = {}
    for table in TABLES:
        total = 0
        done = 0
        for i, ts_code in enumerate(codes, 1):
            # 请求带重试 (防止代理卡死)
            df = None
            for attempt in range(retry + 1):
                try:
                    df = getattr(pro, table)(
                        ts_code=ts_code,
                        start_date="19900101",
                        end_date="19991231",
                    )
                    break
                except Exception as e:
                    logger.warning(f"  {table} {ts_code} 尝试{attempt+1}失败: {e}")
                    time.sleep(5)
            time.sleep(API_SLEEP)

            if df is None or df.empty:
                done += 1
                continue

            # 归一化日期
            for c in ("ann_date", "f_ann_date", "end_date"):
                if c in df.columns:
                    df[c] = df[c].map(_to_date)

            # 只留表内列
            with PgClient() as pg:
                cols = {c["name"] for c in pg.get_columns("fin", table)}
                keep = [c for c in df.columns if c in cols]
                df2 = df[keep]
                pg.upsert_df(df2, "fin", table,
                             conflict_keys=["ts_code", "end_date", "report_type"])
                total += len(df2)
            done += 1

            if i % 100 == 0:
                logger.info(f"  {table}: 已处理 {i}/{len(codes)}, 累计 {total} 行")

        result[table] = total
        logger.info(f"完成 {table}: 补充 {total} 行")

    return result


def main():
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)
    result = backfill_90s(pro)
    print(f"90s 财务补充完成: {result}")


if __name__ == "__main__":
    main()