"""
Tushare full historical daily bars -> PostgreSQL sync module (2000-credit tier)

Features:
- Stock pool: main board + ChiNext + STAR (from stock_basic; excludes BSE/B-shares/ST/delisted)
- Full backfill: daily (daily bars) / adj_factor (adjustment factors) / daily_basic (daily valuation)
- Resumable: based on the stock.sync_meta watermark + actual data in the
  table, safe to rerun repeatedly
- Trading calendar: synced to PG via trade_cal, for K-line analysis

Permissions (2000 credits):
- daily        200 calls/min, one trade_date call returns the whole market
- adj_factor   200 calls/min
- daily_basic  200 calls/min

Usage (CLI):
    python -m tools.market.sync_tushare --start 20000101
    python -m tools.market.sync_tushare --tables daily,adj_factor

[Boundary constraint] This module only touches the Tushare API and SQL data
read/write on the remote PostgreSQL; it performs no server-level operations
(no SSH / no remote shell / no server config changes).
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import T_SYNC_META, T_TRADE_CALENDAR, init_schema
import core.net  # noqa: F401  (force direct connection, bypass flaky proxy)

logger = get_logger(__name__)

# Board whitelist (using Tushare's market field)
ALLOWED_MARKETS = ("主板", "创业板", "科创板")
# Excluded prefixes: BSE (8/4), B-shares (200/900)
EXCLUDE_SYMBOL_PREFIX = ("8", "4", "200", "900")

# Request interval (seconds): 200 calls/min -> 0.35s interval leaves headroom
API_SLEEP = 0.35
# How many trading days per batch write (one day ~5000 rows, a 10-day batch ~50k rows)
BATCH_TRADE_DAYS = 10

# Module-level cache
_WHITELIST_CACHE: Optional[set] = None
_TRADING_DAYS_CACHE: Optional[List[date]] = None


def _to_date(v) -> date:
    """Normalize to a date object (accepts str/YYYYMMDD, pd.Timestamp, datetime)"""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        if len(v) == 8 and v.isdigit():
            return datetime.strptime(v, "%Y%m%d").date()
        return pd.to_datetime(v).date()
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.date()
    return None


# ============================================================
# Stock pool / trading days
# ============================================================

def get_whitelist(pro=None, use_cache: bool = True,
                  refresh_remote: bool = True) -> set:
    """
    Whitelist ts_code set (main board + ChiNext + STAR; excludes BSE/B-shares/ST/delisted)
    Source priority: memory cache -> PG stock_basic -> Tushare remote
    """
    global _WHITELIST_CACHE
    if use_cache and _WHITELIST_CACHE is not None:
        return _WHITELIST_CACHE

    # PG cache (the stock_basic table itself is the whitelist; no status filter needed)
    try:
        with PgClient() as pg:
            if pg.table_exists("stock", "stock_basic"):
                rows = pg.fetch_all(
                    "SELECT ts_code FROM stock.stock_basic"
                )
                if rows:
                    _WHITELIST_CACHE = {r["ts_code"] for r in rows}
                    logger.info(f"白名单从 PG 加载: {len(_WHITELIST_CACHE)} 只")
                    return _WHITELIST_CACHE
    except Exception as e:
        logger.warning(f"从 PG 读白名单失败: {e}")

    if not refresh_remote:
        return set()

    if pro is None:
        import tushare as ts
        pro = ts.pro_api(get_config().tushare_token)

    df = pro.stock_basic(
        exchange="", list_status="L",
        fields="ts_code,symbol,name,area,industry,market,list_date,delist_date,is_hs,status",
    )
    if df is None or df.empty:
        logger.warning("stock_basic 返回为空")
        return set()

    before = len(df)
    # Board filter
    df = df[df["market"].isin(ALLOWED_MARKETS)]
    # Symbol prefix filter (BSE 8/4, B-shares 200/900)
    df = df[~df["symbol"].str.startswith(EXCLUDE_SYMBOL_PREFIX)]
    # Name filter ST/*ST/delisted
    df = df[~df["name"].str.contains("ST", case=False, na=False)]
    df = df[~df["name"].str.contains("退", na=False)]

    codes = set(df["ts_code"].tolist())
    logger.info(
        f"白名单(远程): {before} -> {len(codes)} "
        f"(主板/创业板/科创板, 剔除北交/B股/ST/退市)"
    )

    # Persist to PG
    try:
        with PgClient() as pg:
            pg.upsert_df(df, "stock", "stock_basic",
                         conflict_keys=["ts_code"])
            logger.info(f"stock_basic 落地 PG: {len(df)} 行")
    except Exception as e:
        logger.warning(f"白名单落地 PG 失败: {e}")

    _WHITELIST_CACHE = codes
    return codes


def filter_allowed_codes(df: pd.DataFrame) -> pd.DataFrame:
    """Filter a DataFrame down to the whitelist"""
    if "ts_code" not in df.columns or df.empty:
        return df
    wl = get_whitelist(use_cache=True, refresh_remote=True)
    if wl:
        return df[df["ts_code"].isin(wl)]
    return df


def sync_trade_calendar(pro, start: date, end: date) -> int:
    """Sync the trading calendar (SSE) to PG"""
    df = pro.trade_cal(
        exchange="SSE",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    if df is None or df.empty:
        return 0
    df["cal_date"] = df["cal_date"].map(_to_date)
    df["pretrade_date"] = df["pretrade_date"].map(_to_date)
    # Fill nulls (newer pandas fillna rejects None values)
    df = df.where(pd.notna(df), None)

    with PgClient() as pg:
        pg.upsert_df(df, "stock", "trade_calendar",
                     conflict_keys=["exchange", "cal_date"])
    global _TRADING_DAYS_CACHE
    _TRADING_DAYS_CACHE = None
    logger.info(f"交易日历同步完成: {len(df)} 行")
    return len(df)


def get_trading_days_in_range(pg: PgClient, start: date, end: date) -> List[date]:
    """Read trading days within a range from PG trade_calendar (ascending)"""
    rows = pg.fetch_all(
        f"SELECT cal_date FROM {T_TRADE_CALENDAR} "
        "WHERE is_open=1 AND cal_date BETWEEN %s AND %s ORDER BY cal_date",
        (start, end),
    )
    return [r["cal_date"] for r in rows]


# ============================================================
# Sync watermark
# ============================================================

def get_sync_progress(table: str, pg: PgClient) -> Optional[date]:
    row = pg.fetch_one(
        f"SELECT latest_date FROM {T_SYNC_META} WHERE table_name=%s",
        (table,),
    )
    return row["latest_date"] if row else None


def set_sync_progress(table: str, latest_date: date, total: int, pg: PgClient) -> None:
    pg.execute(
        f"""INSERT INTO {T_SYNC_META} (table_name, latest_date, total_rows, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (table_name) DO UPDATE
            SET latest_date = EXCLUDED.latest_date,
                total_rows  = EXCLUDED.total_rows,
                updated_at  = now()""",
        (table, latest_date, total),
    )


# ============================================================
# Backfill core
# ============================================================

def backfill_table(
    pro,
    table: str,
    api_name: str,
    keys: Sequence[str],
    fields: Sequence[str],
    start: date,
    end: date,
) -> int:
    """
    Generic backfiller: fetch per trading day -> batch write to PG

    Args:
        table: target table name ('daily' / 'adj_factor' / 'daily_basic')
        api_name: Tushare API function name
        keys: table primary key columns
        fields: columns to write (a subset of the columns Tushare returns)
    """
    # Resume: based on the table's actual MAX(trade_date) (more reliable than sync_meta)
    with PgClient() as pg:
        actual_max = pg.fetch_one(
            f"SELECT MAX(trade_date) AS m FROM stock.{table} "
            "WHERE trade_date <= %s",
            (end,),
        )
        actual_max_d = actual_max["m"] if actual_max and actual_max["m"] else None

    start_for_sync = start
    if actual_max_d is not None:
        if actual_max_d >= end:
            logger.info(
                f"断点续传: 表 {table} 区间内已有数据至 {actual_max_d} "
                f">= 目标 {end}, 跳过"
            )
            return 0
        if actual_max_d >= start:
            start_for_sync = actual_max_d + timedelta(days=1)
            logger.info(
                f"断点续传: 表 {table} 从 {start_for_sync} 继续补齐"
            )
        else:
            logger.info(f"断点续传: 表 {table} 区间内无数据, 执行全量回填")
    else:
        logger.info(f"断点续传: 表 {table} 无历史数据, 执行全量回填")

    # Trading day list
    with PgClient() as pg:
        trading_days = get_trading_days_in_range(pg, start_for_sync, end)
    if not trading_days:
        logger.warning(f"{table}: 区间内无交易日, 跳过")
        return 0
    logger.info(f"回填 {table}: {start_for_sync}~{end}, {len(trading_days)} 个交易日")

    api_func = getattr(pro, api_name)
    total = 0
    batch_dfs: List[pd.DataFrame] = []
    last_synced: Optional[date] = None

    for i, day in enumerate(trading_days, 1):
        day_s = day.strftime("%Y%m%d")
        try:
            df = api_func(trade_date=day_s)
            time.sleep(API_SLEEP)
        except Exception as e:
            logger.warning(f"  {table} {day_s} 抓取失败: {e}")
            time.sleep(2)
            continue

        if df is None or df.empty:
            continue

        df = filter_allowed_codes(df)
        if df.empty:
            continue

        # Keep only the needed columns
        keep = [f for f in fields if f in df.columns]
        df = df[keep]
        if "trade_date" in df.columns:
            df["trade_date"] = df["trade_date"].map(_to_date)

        batch_dfs.append(df)
        last_synced = day

        if len(batch_dfs) >= BATCH_TRADE_DAYS:
            n = _flush_batch(batch_dfs, table, keys)
            total += n
            logger.info(
                f"  {table}: 第 {i}/{len(trading_days)} 天, 批次写入 {n} 行 "
                f"(累计 {total})"
            )
            batch_dfs = []
            if last_synced is not None:
                with PgClient() as pg:
                    set_sync_progress(table, last_synced, total, pg)

    # Flush the leftover batch after the loop ends
    # (fix: when the last trading day's data is not yet published (before
    # 15-16:00), the previous i==len-based flush could be short-circuited by
    # `continue`, leaving a whole batch never written)
    if batch_dfs:
        n = _flush_batch(batch_dfs, table, keys)
        total += n
        logger.info(f"  {table}: 末批写入 {n} 行 (累计 {total})")
        if last_synced is not None:
            with PgClient() as pg:
                set_sync_progress(table, last_synced, total, pg)

    logger.info(f"回填完成 {table}: 累计 {total} 行")
    return total


def _flush_batch(dfs: List[pd.DataFrame], table: str,
                 keys: Sequence[str]) -> int:
    """Merge this batch's DataFrames and write to PG, returns the row count written"""
    if not dfs:
        return 0
    df = pd.concat(dfs, ignore_index=True)
    if df.empty:
        return 0
    with PgClient() as pg:
        pg.upsert_df(df, "stock", table, conflict_keys=list(keys))
    return len(df)


# ============================================================
# Full pipeline
# ============================================================

# Per-table config: table name -> (Tushare API name, primary key, columns)
TABLE_CONFIG = {
    "daily": (
        "daily",
        ["trade_date", "ts_code"],
        ["trade_date", "ts_code", "open", "high", "low", "close",
         "pre_close", "change", "pct_chg", "vol", "amount"],
    ),
    "adj_factor": (
        "adj_factor",
        ["ts_code", "trade_date"],
        ["ts_code", "trade_date", "adj_factor"],
    ),
    "daily_basic": (
        "daily_basic",
        ["trade_date", "ts_code"],
        ["trade_date", "ts_code", "close", "turnover_rate", "turnover_rate_f",
         "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio",
         "dv_ttm", "total_share", "float_share", "free_share", "total_mv",
         "circ_mv"],
    ),
}


class TusharePgSyncer:
    """Tushare -> PG syncer"""

    def __init__(self):
        self.config = get_config()
        if not self.config.tushare_token:
            raise ValueError("未配置 TUSHARE_TOKEN (.env)")
        # Direct connection to the Tushare API, bypassing system proxies
        # (flaky proxies once crashed the backfill)
        import os
        for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(_k, None)
        os.environ.setdefault("NO_PROXY", "*")

    def run(self, start=None, end=None,
            tables: Optional[Iterable[str]] = None) -> Dict[str, int]:
        import tushare as ts
        pro = ts.pro_api(self.config.tushare_token)

        start = _to_date(start) if start else date(2000, 1, 1)
        end = _to_date(end) if end else date.today()
        end = min(end, date.today())

        # 1. Initialize table structure
        with PgClient() as pg:
            init_schema(pg)

        # 2. Whitelist (first run fetches remotely and persists to PG; later runs read PG directly)
        codes = get_whitelist(pro, use_cache=False, refresh_remote=True)
        logger.info(f"白名单股票数: {len(codes)}")

        # 3. Trading calendar
        sync_trade_calendar(pro, start, end)

        # 4. Backfill each table
        tables = set(tables or list(TABLE_CONFIG.keys()))
        result: Dict[str, int] = {}

        for t in tables:
            if t not in TABLE_CONFIG:
                logger.warning(f"未知表: {t}, 可选: {list(TABLE_CONFIG.keys())}")
                continue
            api_name, keys, fields = TABLE_CONFIG[t]
            result[t] = backfill_table(
                pro, t, api_name, keys, fields, start, end
            )

        return result


def run_backfill(start=None, end=None, tables=None) -> Dict[str, int]:
    return TusharePgSyncer().run(start, end, tables)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tushare 全量日K回填到 PostgreSQL"
    )
    parser.add_argument("--start", type=str, default=None,
                        help="起始日期 YYYYMMDD (默认 20000101)")
    parser.add_argument("--end", type=str, default=None,
                        help="结束日期 YYYYMMDD (默认今天)")
    parser.add_argument("--tables", type=str, default="daily,adj_factor,daily_basic",
                        help="要同步的表, 逗号分隔 (默认全部)")
    args = parser.parse_args()

    tbls = [t.strip() for t in args.tables.split(",") if t.strip()]
    print(f"开始回填: start={args.start or '20000101'}, end={args.end or 'today'}, "
          f"tables={tbls}")
    result = run_backfill(args.start, args.end, tbls)
    print(f"回填完成: {result}")