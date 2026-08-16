"""
申万 (SW2021) 行业分类同步
- index_classify: 一级/二级行业定义 (31 L1 + 134 L2 + L3)
- stock_industry: 个股 -> 二级行业成分映射

注意: Tushare 无"东财行业", 申万是 A 股权威行业标准.
用法:
    python -m tools.market.sync_industry
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import DDL_INDEX_CLASSIFY, DDL_STOCK_INDUSTRY

logger = get_logger(__name__)

API_SLEEP = 1.3  # index_member 接口限频较低

SRC = "SW2021"


def sync_classify(pro) -> int:
    """同步行业分类表 (全部层级)"""
    df = pro.index_classify(level="L1", src=SRC)
    all_df = df
    time.sleep(API_SLEEP)
    for lv in ["L2", "L3"]:
        try:
            dfx = pro.index_classify(level=lv, src=SRC)
            all_df = _concat(all_df, dfx)
            time.sleep(API_SLEEP)
        except Exception as e:
            logger.warning(f"L{lv} 拉取失败: {e}")

    if all_df is None or all_df.empty:
        logger.warning("分类为空")
        return 0

    with PgClient() as pg:
        pg.upsert_df(all_df, "stock", "index_classify",
                     conflict_keys=["index_code"])
    logger.info(f"行业分类同步完成: {len(all_df)} 条 (L1+L2+L3)")
    return len(all_df)


def sync_members(pro) -> int:
    """同步二级行业成分股"""
    with PgClient() as pg:
        l2_codes = pg.fetch_all(
            "SELECT index_code FROM stock.index_classify "
            "WHERE level='L2' AND is_pub=1"
        )
    codes = [r["index_code"] for r in l2_codes]
    logger.info(f"共 {len(codes)} 个二级行业待同步成分")

    total = 0
    for i, code in enumerate(codes, 1):
        try:
            df = pro.index_member(index_code=code)
            time.sleep(API_SLEEP)
        except Exception as e:
            logger.warning(f"  {code} 成分拉取失败: {e}")
            time.sleep(3)
            continue

        if df is None or df.empty:
            continue
        with PgClient() as pg:
            pg.upsert_df(df, "stock", "stock_industry",
                         conflict_keys=["index_code", "con_code"])
            total += len(df)

        if i % 20 == 0:
            logger.info(f"  已处理 {i}/{len(codes)} 行业, 累计 {total} 成分")

    logger.info(f"成分映射完成: {total} 条")
    return total


def _concat(a, b):
    if a is None or a.empty:
        return b
    if b is None or b.empty:
        return a
    import pandas as pd
    return pd.concat([a, b], ignore_index=True)


def main():
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)

    with PgClient() as pg:
        pg.execute(DDL_INDEX_CLASSIFY)
        pg.execute(DDL_STOCK_INDUSTRY)

    n1 = sync_classify(pro)
    n2 = sync_members(pro)
    print(f"=== 完成: 分类 {n1} 条, 成分映射 {n2} 条 ===")


if __name__ == "__main__":
    main()