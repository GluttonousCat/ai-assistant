"""
Tushare main-business composition (fina_mainbz) -> PostgreSQL sync

Features:
- Full backfill: one call per stock returns all report periods x all
  dimensions (bz_code carries the P=product/D=region/I=industry tag)
- Rate-limit retry: wait and retry on per-minute throttling so no stock is
  lost; insufficient credits aborts immediately
- Resumable: progress recorded in fin.sync_meta (table_name=fina_mainbz),
  safe to rerun repeatedly
- Incremental mode: re-pull only stocks whose reports were announced within
  the last N days (driven by income.ann_date, idempotent upsert)

Data notes:
- Main-business composition = the report's revenue breakdown by
  product/region/industry (revenue/cost/profit), amounts in yuan
- The API returns neither ann_date nor update_flag; one call returns the
  company's full history and all dimensions
- Cleaning of dimension header rows (product/industry/region), total rows and
  colon-indented sub-items lives in the view fin.v_main_biz

Usage (CLI):
    python -m tools.market.sync_mainbz                     # full backfill (resumable)
    python -m tools.market.sync_mainbz --limit 5           # first 5 stocks (test)
    python -m tools.market.sync_mainbz --mode incremental  # incremental (stocks with recently announced reports)
    python -m tools.market.sync_mainbz --full              # ignore checkpoint, full rerun
"""
from __future__ import annotations

import argparse
import time
from typing import List, Optional

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import init_fin_schema
from tools.market.sync_financial import (
    _to_date,
    get_fin_progress,
    get_whitelist_codes,
    set_fin_progress,
)
import core.net  # noqa: F401  (force direct connection, bypass flaky proxy)

logger = get_logger("sync_mainbz")

API_SLEEP = 0.35          # same throttle as sync_financial
TABLE = "fina_mainbz"     # watermark key name in fin.sync_meta
PK_COLS = ["ts_code", "end_date", "bz_code", "bz_item"]

# Actual API output fields (no ann_date/update_flag; bz_code is the dimension tag)
MAINBZ_FIELDS = [
    "ts_code", "end_date", "bz_item", "bz_code",
    "bz_sales", "bz_profit", "bz_cost", "curr_type",
]


def fetch_stock_mainbz(pro, ts_code: str) -> pd.DataFrame:
    """Fetch all report periods x all dimensions for one stock (waits and
    retries on throttling)"""
    for attempt in range(1, 4):
        try:
            return pro.fina_mainbz(
                ts_code=ts_code,
                fields=",".join(MAINBZ_FIELDS),
            )
        except Exception as e:
            msg = str(e)
            if "积分" in msg or "权限" in msg:
                raise PermissionError(f"fina_mainbz 接口不可用(积分/权限): {e}")
            if attempt < 3:
                wait = 60 if "每分钟" in msg else 5
                logger.warning(
                    f"  {ts_code} 第{attempt}次失败, {wait}s后重试: {msg}")
                time.sleep(wait)
            else:
                logger.warning(f"  {ts_code} 连续3次失败, 跳过: {msg}")
    return pd.DataFrame()


def sync_one_stock(pro, ts_code: str) -> int:
    """Sync one stock, returns stored row count"""
    df = fetch_stock_mainbz(pro, ts_code)
    if df is None or df.empty:
        return 0
    df = df.copy()
    # bz_code dimension-tag cleanup (codes like 455006000 are supplementary
    # dimensions such as sales mode; kept as-is)
    df["bz_code"] = df["bz_code"].astype(str).str.strip()
    df["end_date"] = df["end_date"].map(_to_date)
    df = df[df["bz_item"].notna() & df["end_date"].notna()]
    df["bz_item"] = df["bz_item"].astype(str).str.strip().str.slice(0, 512)
    df = df[~df["bz_item"].isin(("", "nan", "None"))]
    df = df.drop_duplicates(subset=PK_COLS)

    with PgClient() as pg:
        table_cols = {c["name"] for c in pg.get_columns("fin", TABLE)}
    df = df[[c for c in df.columns if c in table_cols]]
    if df.empty:
        return 0

    with PgClient() as pg:
        pg.upsert_df(df, "fin", TABLE, conflict_keys=PK_COLS)
    return len(df)


def backfill_mainbz(pro, codes: List[str], limit: Optional[int] = None,
                    force_full: bool = False,
                    record_progress: bool = True) -> int:
    """Backfill main-business composition (stock by stock, resumable)"""
    est_min = int(len(codes) * 0.5) // 60
    logger.info(f"回填主营构成: {len(codes)} 只 (每股1次调用), 预计约 {est_min} 分钟")

    # Checkpoint: resume after the last ts_code (ignored when force_full=True)
    if not force_full:
        last_code = get_fin_progress(TABLE)
        if last_code and last_code in codes:
            idx = codes.index(last_code)
            codes = codes[idx + 1:]
            logger.info(f"断点续传: 从 {last_code} 之后继续, 剩余 {len(codes)} 只")
        elif last_code:
            logger.info(f"断点续传: 上次进度 {last_code} 已不在白名单, 全量重跑")
    else:
        logger.info("忽略断点, 从头执行")

    if limit:
        codes = codes[:limit]

    total = 0
    empty_cnt = 0
    for i, ts_code in enumerate(codes, 1):
        rows = sync_one_stock(pro, ts_code)
        total += rows
        if rows == 0:
            empty_cnt += 1
        # Incremental mode does not advance the backfill watermark (avoid
        # disturbing the resume checkpoint)
        if record_progress:
            set_fin_progress(TABLE, ts_code, total)
        if i % 50 == 0:
            logger.info(f"  已处理 {i}/{len(codes)} 只, 累计 {total} 行, 无数据 {empty_cnt} 只")
        time.sleep(API_SLEEP)

    logger.info(f"回填完成: {len(codes)} 只, 累计 {total} 行, 无数据 {empty_cnt} 只")
    return total


def get_recent_report_codes(days: int = 14) -> List[str]:
    """Stocks whose reports were announced in the last N days
    (income.ann_date) -- main-business composition is disclosed together with
    the report"""
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT DISTINCT ts_code FROM fin.income "
            "WHERE ann_date >= current_date - %s::int "
            "ORDER BY ts_code",
            (days,),
        )
    return [r["ts_code"] for r in rows]


def run_mainbz(mode: str = "backfill", limit: Optional[int] = None,
               force_full: bool = False, days: int = 14) -> int:
    """Run main-business composition sync (backfill = full resumable /
    incremental = stocks with recent reports)"""
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)

    # Ensure tables + views (idempotent)
    with PgClient() as pg:
        init_fin_schema(pg)

    if mode == "incremental":
        codes = get_recent_report_codes(days)
        logger.info(f"增量模式: 最近{days}天公告财报的股票 {len(codes)} 只")
        if not codes:
            logger.info("无待处理股票")
            return 0
        return backfill_mainbz(pro, codes, limit=limit, force_full=True,
                               record_progress=False)

    codes = get_whitelist_codes()
    if not codes:
        raise RuntimeError("白名单为空, 请先运行行情同步")
    return backfill_mainbz(pro, codes, limit=limit, force_full=force_full)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tushare 主营构成(fina_mainbz) 同步 PostgreSQL")
    parser.add_argument("--mode", choices=["backfill", "incremental"],
                        default="backfill", help="backfill=全量断点续传 / incremental=增量")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理股票数 (测试用)")
    parser.add_argument("--days", type=int, default=14,
                        help="增量模式回看天数")
    parser.add_argument("--full", action="store_true",
                        help="忽略断点全量重跑")
    args = parser.parse_args()

    print(f"开始主营构成同步: mode={args.mode}, limit={args.limit}, days={args.days}")
    try:
        n = run_mainbz(mode=args.mode, limit=args.limit,
                       force_full=args.full, days=args.days)
        print(f"主营构成同步完成: {n} 行")
    except PermissionError as e:
        logger.error(str(e))
        raise SystemExit(1)
