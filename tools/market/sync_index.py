# -*- encoding: utf-8 -*-
"""
指数日K同步 (从 tushare index_daily 拉取核心指数, 入库 stock.index_daily)

用法:
    python -m tools.market.sync_index            # 全量同步 (2020 起)
    python -m tools.market.sync_index --start 20260101  # 增量
"""
from __future__ import annotations

import argparse
from datetime import datetime
from typing import Dict

import pandas as pd

from core.logger import get_logger
from storage.pg import PgClient

logger = get_logger("market.sync_index")

# 核心指数: 上证指数 / 沪深300 / 创业板指 / 上证50 / 中证500
INDEXES: Dict[str, str] = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "399006.SZ": "创业板指",
    "000016.SH": "上证50",
    "000905.SH": "中证500",
}

DDL = """
CREATE TABLE IF NOT EXISTS stock.index_daily (
    trade_date DATE NOT NULL,
    ts_code    VARCHAR(16) NOT NULL,
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION,
    pct_chg    DOUBLE PRECISION,
    vol        DOUBLE PRECISION,
    amount     DOUBLE PRECISION,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_index_daily_code ON stock.index_daily (ts_code, trade_date);
"""


def sync_indexes(pro, start: str, end: str) -> Dict[str, int]:
    """同步核心指数日K, 返回 {code: 行数}"""
    total = {}
    with PgClient() as pg:
        pg.execute("CREATE SCHEMA IF NOT EXISTS stock")
        for stmt in DDL.strip().split(";"):
            s = stmt.strip()
            if s:
                pg.execute(s)
        for code, name in INDEXES.items():
            try:
                df = pro.index_daily(ts_code=code, start_date=start, end_date=end)
            except Exception as e:
                logger.warning(f"{name}({code}) 拉取失败: {e}")
                continue
            if df is None or df.empty:
                logger.info(f"{name}({code}): 无数据")
                total[code] = 0
                continue
            df = df.rename(columns={"vol": "vol"})
            df = df[["trade_date", "ts_code", "open", "high", "low", "close",
                     "pct_chg", "vol", "amount"]].copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            for c in ["open", "high", "low", "close", "pct_chg", "vol", "amount"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            n = pg.upsert_df(df, "stock", "index_daily",
                             conflict_keys=["trade_date", "ts_code"])
            total[code] = int(n)
            logger.info(f"{name}({code}): {n} 行")
    return total


def main():
    parser = argparse.ArgumentParser(description="指数日K同步")
    parser.add_argument("--start", type=str, default="20200101", help="开始日期 YYYYMMDD")
    parser.add_argument("--end", type=str, default=None, help="结束日期 YYYYMMDD (默认今天)")
    args = parser.parse_args()
    end = args.end or datetime.now().strftime("%Y%m%d")

    import tushare as ts
    pro = ts.pro_api()
    result = sync_indexes(pro, args.start, end)
    print("同步完成:", result)


if __name__ == "__main__":
    main()