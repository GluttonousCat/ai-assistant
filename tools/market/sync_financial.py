"""
Tushare financial data -> PostgreSQL sync module

Features:
- Full backfill: income (income statement) / balancesheet (balance sheet) /
  cashflow (cash flow statement) / fina_indicator (financial indicators) /
  dividend (dividends & splits)
- The four financial tables' columns are 1:1 with Tushare's official output
  parameters (income 94 / balancesheet 158 / cashflow 97 / fina_indicator 167
  fields; dividend 16 fields)
- Fetch field lists are not hand-maintained: columns are reflected from the
  actual tables (information_schema); the table structure is the single source
  of truth (the old version hand-maintained duplicate DDL and request lists,
  which drifted into 10+16 misnamed fields that the API silently dropped ->
  whole columns all NULL)
- dividend daily increment goes through run_dividend_daily: per-day
  whole-market batches over three channels (not per stock)
- Data source: per-stock pulls (income-family APIs work at 2000 credits;
  income_vip needs 5000)
- report_type=1 (consolidated statements, latest)
- Resumable: stock by stock by ts_code, progress recorded in sync_meta, safe
  to rerun repeatedly

Usage (CLI):
    python -m tools.market.sync_financial                   # all 4 tables
    python -m tools.market.sync_financial --tables income   # income only
    python -m tools.market.sync_financial --limit 100       # first 100 stocks (test)
"""
from __future__ import annotations

import argparse
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import psycopg2

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import init_fin_schema
import core.net  # noqa: F401  (force direct connection, bypass flaky proxy)

logger = get_logger(__name__)

API_SLEEP = 0.35  # 200 calls/min -> 0.35s interval leaves headroom

# PG disconnect/server restart: one connection per stock, so retrying a single
# stock reconnects (pitfall table: long batch jobs resume per work unit)
PG_DOWN = (psycopg2.OperationalError, psycopg2.InterfaceError)


class TushareHangError(RuntimeError):
    """API call hangs (DNS/half-open connections and other blocks not covered
    by the requests timeout): several consecutive hangs are judged as network
    outage and abort this round quickly"""


_hang_streak = 0


def _api_call(pro, api_name: str, timeout: int = 90, **params) -> pd.DataFrame:
    """
    Tushare API call with a hard wall-clock bound: daemon thread + join timeout.
    The SDK's built-in timeout=30 only covers socket read/write; blocking calls
    such as DNS resolution (getaddrinfo) are not bound by it, so network
    flakiness can hang forever (on 2026-10-11 the cashflow backfill actually
    hung 10+ minutes with no error). A hung daemon thread is abandoned, not
    reaped; 5 consecutive hangs are treated as network outage and raise
    TushareHangError to abort this round (the checkpoint is already saved in
    fin.sync_meta; just rerun once the network recovers).
    """
    global _hang_streak
    box: Dict[str, Any] = {}

    def _call():
        try:
            box["df"] = getattr(pro, api_name)(**params)
        except Exception as e:  # noqa: BLE001  carry the fail-fast exception back to the main thread
            box["err"] = e

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _hang_streak += 1
        if _hang_streak >= 5:
            raise TushareHangError(
                f"{api_name} 连续 {_hang_streak} 次拉取挂死(疑似断网), 终止本轮同步 —— 断点已保存")
        logger.warning(f"{api_name} {params} 拉取挂死 {timeout}s, 弃置 "
                       f"(连击 {_hang_streak}/5)")
        return pd.DataFrame()
    _hang_streak = 0
    if "err" in box:
        raise box["err"]
    return box.get("df", pd.DataFrame())

# Tushare API name per table (table name == API name); fetched fields come from
# table_fields() reflection. disclosure_date is not listed here: its API does
# not support the per-stock start_date skeleton param, so it is pulled in
# whole-market batches by report period (see backfill_disclosure_periods)
FIN_TABLE_APIS = {
    "income": "income",
    "balancesheet": "balancesheet",
    "cashflow": "cashflow",
    "fina_indicator": "fina_indicator",
    "dividend": "dividend",
    "fina_audit": "fina_audit",
}

# Upsert conflict keys: the default is the primary key of the three main
# financial statement tables; other tables override per table
FIN_CONFLICT_KEYS = {
    "dividend": ["ts_code", "end_date", "ann_date", "div_proc"],
    "fina_audit": ["ts_code", "end_date", "ann_date"],
    "disclosure_date": ["ts_code", "end_date"],
}

# Date normalization: extra columns per table beyond the base three
DATE_NORM_EXTRA = {
    "dividend": ("record_date", "ex_date", "pay_date",
                 "div_listdate", "imp_ann_date", "base_date"),
    "disclosure_date": ("pre_date", "actual_date", "modify_date"),
}

# Per-call row cap of each API (a Tushare limit; exceeding it truncates
# silently): affected APIs page backwards by report period in fetch_stock_fin
# until exhausted; income is uncapped, and the other tables' full per-stock
# history stays under the cap
FIN_PAGE_CAP = {
    "balancesheet": 100,
    "cashflow": 100,
    "fina_indicator": 100,
}


def _page_min_end(df: pd.DataFrame) -> Optional[date]:
    """Oldest report period on the page (end_date column cast to date; returns
    None if the column is missing/all empty, which stops paging)"""
    if "end_date" not in df.columns or df.empty:
        return None
    vals = [d for d in (_to_date(v) for v in df["end_date"]) if d]
    return min(vals) if vals else None

_warned_dropped: set = set()


def table_fields(table: str) -> List[str]:
    """Reflect the fetch field list from the actual table (excluding local
    created_at); new columns need no code change - the table structure is the
    request list"""
    with PgClient() as pg:
        return [c["name"] for c in pg.get_columns("fin", table)
                if c["name"] != "created_at"]


def _filter_to_table(df: pd.DataFrame, table: str, table_cols: set) -> pd.DataFrame:
    """Keep only columns that exist in the table; warn (once per group) when
    the API returns out-of-table columns. Should normally never trigger after
    field reflection - pure safety net."""
    dropped = [c for c in df.columns if c not in table_cols]
    if dropped:
        key = (table, tuple(dropped))
        if key not in _warned_dropped:
            _warned_dropped.add(key)
            logger.warning(f"{table}: API 返回了表内不存在的列, 已丢弃: {dropped}")
    return df[[c for c in df.columns if c in table_cols]]

def get_whitelist_codes() -> List[str]:
    """Read the whitelist ts_code list from PG stock_basic"""
    from tools.market.sync_tushare import get_whitelist
    codes = get_whitelist(use_cache=True, refresh_remote=False)
    return sorted(codes)


def get_fin_progress(table: str) -> Optional[str]:
    """Last synced ts_code (watermark, for resuming)"""
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT last_code FROM fin.sync_meta WHERE table_name=%s",
            (table,),
        )
    return row["last_code"] if row else None


def set_fin_progress(table: str, last_code: str, total: int) -> None:
    with PgClient() as pg:
        pg.execute(
            """INSERT INTO fin.sync_meta (table_name, last_code, total_rows, updated_at)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (table_name) DO UPDATE
               SET last_code = EXCLUDED.last_code,
                   total_rows = EXCLUDED.total_rows,
                   updated_at = now()""",
            (table, last_code, total),
        )


def _normalize_date_col(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Convert a date column (YYYYMMDD int/str) to date objects"""
    if col in df.columns:
        df[col] = df[col].map(
            lambda v: _to_date(v) if pd.notna(v) and v != "" else None
        )
    return df


def _to_date(v) -> Optional[date]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s == "nan":
        return None
    if len(s) == 8 and s.isdigit():
        return datetime.strptime(s, "%Y%m%d").date()
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def fetch_stock_fin(pro, api_name: str, ts_code: str, fields: List[str],
                    start: str = "19900101") -> pd.DataFrame:
    """
    Fetch one stock's full history from one API (hangs are bounded by
    _api_call's wall clock).

    balancesheet/cashflow/fina_indicator APIs cap a single call at 100 rows, so
    long history gets silently truncated (old stocks only return the latest N
    periods; earlier report periods were never stored): for these APIs page
    backwards by report period end_date until exhausted -- the first page
    omits end_date to get the newest page, then end_date = one day before the
    oldest period on the current page, until a page is empty / shorter than
    the cap / no longer advances; upsert is idempotent, so overlap between
    pages is harmless.
    """
    cap = FIN_PAGE_CAP.get(api_name)
    try:
        first = _api_call(pro, api_name, ts_code=ts_code,
                          start_date=start, fields=",".join(fields))
    except TushareHangError:
        raise
    except Exception as e:
        logger.warning(f"  {api_name} {ts_code} 抓取失败: {e}")
        time.sleep(2)
        return pd.DataFrame()
    if first is None or first.empty or not cap or len(first) < cap:
        return first
    frames = [first]
    seen_min = _page_min_end(first)
    while seen_min:
        cursor = (seen_min - timedelta(days=1)).strftime("%Y%m%d")
        page = _api_call(pro, api_name, ts_code=ts_code, start_date=start,
                         end_date=cursor, fields=",".join(fields))
        if page is None or page.empty:
            break
        frames.append(page)
        new_min = _page_min_end(page)
        # Short page = older data exhausted; no forward progress = fallback for
        # same-period clustering (rows per period << 100, theoretically unreachable)
        if len(page) < cap or not new_min or new_min >= seen_min:
            break
        seen_min = new_min
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(subset=[k for k in
                                      FIN_CONFLICT_KEYS.get(api_name,
                                                            ["ts_code", "end_date", "report_type"])
                                      if k in df.columns])


def _upsert_stock_fin(pro, table: str, api_name: str, fields: List[str],
                      ts_code: str, start: str = "19900101") -> int:
    """Fetch + store work unit for one stock (returns stored row count); PG
    disconnects are retried per stock by the caller"""
    df = fetch_stock_fin(pro, api_name, ts_code, fields, start=start)
    if df is None or df.empty:
        return 0
    for c in ("ann_date", "f_ann_date", "end_date") + DATE_NORM_EXTRA.get(table, ()):
        _normalize_date_col(df, c)
    # Skip rows with NULL PK columns (observed: some early dividend rows in
    # stages like "shareholder proposal" have NULL ann_date)
    keys = FIN_CONFLICT_KEYS.get(table, ["ts_code", "end_date", "report_type"])
    bad = df[[k for k in keys if k in df.columns]].isna().any(axis=1)
    if bad.any():
        logger.warning(f"{table} {ts_code}: {int(bad.sum())} 行 PK 列含空值, 已跳过")
        df = df[~bad]
        if df.empty:
            return 0
    with PgClient() as pg:
        table_cols = {c["name"] for c in pg.get_columns("fin", table)}
    df = _filter_to_table(df, table, table_cols)
    with PgClient() as pg:
        pg.upsert_df(
            df, "fin", table,
            conflict_keys=FIN_CONFLICT_KEYS.get(
                table, ["ts_code", "end_date", "report_type"]),
        )
    return len(df)


def fetch_recent_financial(pro, table: str, codes: List[str],
                           start: str) -> int:
    """
    Incremental fetch: filter by ann_date (announce date), only pulling
    reports announced after `start`. Report announcements are sporadic events
    (quarterly/annual/corrections), so one run per day stays current.
    Returns new row count. Per-API rate limit is 200 calls/min, shared
    throttling with the full backfill.
    """
    api_name = FIN_TABLE_APIS[table]
    fields = table_fields(table)
    # fina_indicator's start_date/end_date mean REPORT PERIOD, not announce
    # date (the income family uses announce date): an announce-date window
    # start would filter out whole blocks of earlier-period rows (observed:
    # 2026Q3 entirely missing -- report period 0930 < window start 1013). For
    # this table the window is shifted into a report-period floor (pushed back
    # 120 days to cover report periods announced inside the window); row
    # volume is still only a few recent periods, no added weight.
    fetch_start = (datetime.strptime(start, "%Y%m%d") - timedelta(days=120)
                   ).strftime("%Y%m%d") if table == "fina_indicator" else start
    total = 0
    for i, ts_code in enumerate(codes, 1):
        rows = 0
        for attempt in range(1, 4):
            try:
                rows = _upsert_stock_fin(pro, table, api_name, fields,
                                         ts_code, start=fetch_start)
                break
            except PG_DOWN as e:
                logger.warning(f"{table} {ts_code} PG 断连 "
                               f"({attempt}/3): {str(e)[:80]}")
                time.sleep(10 * attempt)
        total += rows
        if i % 200 == 0:
            logger.info(f"  [增量] {table}: 已处理 {i}/{len(codes)} 只, 新增 {total} 行")
        time.sleep(API_SLEEP)
    logger.info(f"[增量] {table}: 新公告财报 {total} 行 (start={start})")
    return total


def run_financial_incremental(days: int = 10) -> Dict[str, int]:
    """
    Daily increment: pull reports announced in the last N days (covers
    weekend/holiday announcement pile-ups). Per-stock pulls stay within the
    API rate limit; whole market ~5000 stocks x 4 tables takes ~20 minutes.
    """
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)

    with PgClient() as pg:
        init_fin_schema(pg)

    codes = get_whitelist_codes()
    if not codes:
        raise RuntimeError("白名单为空, 请先运行行情同步")

    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    result: Dict[str, int] = {}
    for t in FIN_TABLE_APIS:
        result[t] = fetch_recent_financial(pro, t, codes, start)
    return result


# ---- dividend daily increment: event-level pulls in per-day whole-market
# batches over three channels (vs 5000 per-stock calls; events are sparse) ----

DIVIDEND_DATE_COLS = ("ann_date", "end_date", "record_date", "ex_date",
                      "pay_date", "div_listdate", "imp_ann_date", "base_date")


def _fetch_dividend_by_date(pro, param: str, d: str,
                            fields: List[str]) -> pd.DataFrame:
    """Fetch whole-market dividends by a single-day param (one of
    ann_date/imp_ann_date/ex_date), retry up to 3 times on API failure"""
    for attempt in range(1, 4):
        try:
            return _api_call(pro, "dividend", **{param: d},
                             fields=",".join(fields))
        except TushareHangError:
            raise
        except Exception as e:
            logger.warning(f"dividend {param}={d} 抓取失败 ({attempt}/3): {e}")
            time.sleep(2 * attempt)
    return pd.DataFrame()


def sync_dividend_daily(pro, days: int = 2) -> int:
    """
    dividend daily increment: pull the whole market per day over three
    channels (ann_date new announcements / imp_ann_date implementation
    announcements / ex_date ex-dividend catch-all). Upsert is idempotent, so
    duplicate rows across channels and days are safe. A single day's
    whole-market dividend events are << 2000 rows, no paging; returns the
    cumulative processed row count (including idempotent duplicates).
    """
    fields = table_fields("dividend")
    conflict = FIN_CONFLICT_KEYS["dividend"]
    today = date.today()
    total = 0
    with PgClient() as pg:
        table_cols = {c["name"] for c in pg.get_columns("fin", "dividend")}
    for back in range(days):
        d = (today - timedelta(days=back)).strftime("%Y%m%d")
        for param in ("ann_date", "imp_ann_date", "ex_date"):
            df = _fetch_dividend_by_date(pro, param, d, fields)
            if df is None or df.empty:
                continue
            if len(df) >= 2000:
                logger.warning(f"dividend {param}={d} 返回 {len(df)} 行触及单次上限, 可能截断")
            for c in DIVIDEND_DATE_COLS:
                _normalize_date_col(df, c)
            # Rows with NULL PK columns (theoretically impossible; guards a
            # NOT NULL violation from aborting the whole batch)
            bad = df[["ts_code", "end_date", "ann_date", "div_proc"]].isna().any(axis=1)
            if bad.any():
                logger.warning(f"dividend {param}={d}: {int(bad.sum())} 行 PK 列含空值, 已跳过")
                df = df[~bad]
                if df.empty:
                    continue
            df = _filter_to_table(df, "dividend", table_cols)
            done = False
            for attempt in range(1, 4):
                try:
                    with PgClient() as pg:
                        pg.upsert_df(df, "fin", "dividend", conflict_keys=conflict)
                    done = True
                    break
                except PG_DOWN as e:
                    logger.warning(f"dividend {param}={d} PG 断连 "
                                   f"({attempt}/3): {str(e)[:80]}")
                    time.sleep(10 * attempt)
            if done:
                total += len(df)
                logger.info(f"[增量] dividend {param}={d}: {len(df)} 行 upsert")
            time.sleep(API_SLEEP)
    logger.info(f"[增量] dividend 完成: {days} 天 x 3 通道, 共 {total} 行 (upsert 幂等含重复)")
    return total


def run_dividend_daily(days: int = 2) -> int:
    """Scheduler entry: ensure-table fallback + per-day increment"""
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)
    with PgClient() as pg:
        init_fin_schema(pg)
    return sync_dividend_daily(pro, days)


# ---- disclosure_date report disclosure schedule: batch backfill by report
# period + per-day two-channel increment (variant of the dividend pattern) ----
# One disclosure-plan row per stock per report period (latest state);
# modify_date is a same-row correction flag, not a new row

DISCLOSURE_DATE_COLS = ("ann_date", "end_date", "pre_date", "actual_date", "modify_date")


def _fetch_disclosure_by(pro, param: str, value: str,
                         fields: List[str]) -> pd.DataFrame:
    """Fetch whole-market disclosure plans by a single param (report period
    end_date, or one day's ann_date/actual_date), retry up to 3 times on API
    failure"""
    for attempt in range(1, 4):
        try:
            return _api_call(pro, "disclosure_date", **{param: value},
                             fields=",".join(fields))
        except TushareHangError:
            raise
        except Exception as e:
            logger.warning(f"disclosure_date {param}={value} 抓取失败 "
                           f"({attempt}/3): {e}")
            time.sleep(2 * attempt)
    return pd.DataFrame()


def _upsert_disclosure(df: pd.DataFrame, table_cols: set, label: str) -> int:
    """Single-batch work unit: normalize + NULL-PK guard + upsert (returns
    stored row count); PG disconnects retried within the batch"""
    if df is None or df.empty:
        return 0
    for c in DISCLOSURE_DATE_COLS:
        _normalize_date_col(df, c)
    bad = df[["ts_code", "end_date"]].isna().any(axis=1)
    if bad.any():
        logger.warning(f"disclosure_date {label}: {int(bad.sum())} 行 PK 列含空值, 已跳过")
        df = df[~bad]
        if df.empty:
            return 0
    df = _filter_to_table(df, "disclosure_date", table_cols)
    for attempt in range(1, 4):
        try:
            with PgClient() as pg:
                pg.upsert_df(df, "fin", "disclosure_date",
                             conflict_keys=FIN_CONFLICT_KEYS["disclosure_date"])
            return len(df)
        except PG_DOWN as e:
            logger.warning(f"disclosure_date {label} PG 断连 "
                           f"({attempt}/3): {str(e)[:80]}")
            time.sleep(10 * attempt)
    return 0


def backfill_disclosure_periods(pro, start_year: int = 1998,
                                end_year: Optional[int] = None) -> int:
    """
    Backfill: one whole-market call per report period (idempotent, safe to
    rerun). The official docs don't state the historical start; empty periods
    are skipped naturally via df.empty; ~111 calls in total finish in
    minutes, no checkpoint needed.
    """
    fields = table_fields("disclosure_date")
    with PgClient() as pg:
        table_cols = {c["name"] for c in pg.get_columns("fin", "disclosure_date")}
    end_year = end_year or date.today().year
    today_s = date.today().strftime("%Y%m%d")
    total = 0
    for year in range(start_year, end_year + 1):
        for md in ("0331", "0630", "0930", "1231"):
            period = f"{year}{md}"
            if period > today_s:
                continue  # future report periods have no scheduled plans yet
            df = _fetch_disclosure_by(pro, "end_date", period, fields)
            if df is None or df.empty:
                continue
            if len(df) >= 6000:
                logger.warning(f"disclosure_date end_date={period} 返回 {len(df)} 行"
                               f"触及单次上限 6000, 可能截断")
            rows = _upsert_disclosure(df, table_cols, f"end_date={period}")
            total += rows
            logger.info(f"[回填] disclosure_date {period}: {rows} 行 upsert")
            time.sleep(API_SLEEP)
    logger.info(f"[回填] disclosure_date 完成: {start_year}-{end_year} 共 {total} 行")
    return total


def sync_disclosure_daily(pro, days: int = 2) -> int:
    """
    Increment: per-day whole-market pulls over two channels (ann_date new
    plan announcements/changes, actual_date actual disclosure landing).
    Upsert is idempotent, so duplicate rows across channels and days are
    safe; returns the cumulative processed row count (including idempotent
    duplicates).
    """
    fields = table_fields("disclosure_date")
    with PgClient() as pg:
        table_cols = {c["name"] for c in pg.get_columns("fin", "disclosure_date")}
    today = date.today()
    total = 0
    for back in range(days):
        d = (today - timedelta(days=back)).strftime("%Y%m%d")
        for param in ("ann_date", "actual_date"):
            df = _fetch_disclosure_by(pro, param, d, fields)
            if df is None or df.empty:
                continue
            rows = _upsert_disclosure(df, table_cols, f"{param}={d}")
            if rows:
                total += rows
                logger.info(f"[增量] disclosure_date {param}={d}: {rows} 行 upsert")
            time.sleep(API_SLEEP)
    logger.info(f"[增量] disclosure_date 完成: {days} 天 x 2 通道, 共 {total} 行 (upsert 幂等含重复)")
    return total


def run_disclosure_daily(days: int = 2) -> int:
    """Scheduler entry: ensure-table fallback + per-day increment"""
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)
    with PgClient() as pg:
        init_fin_schema(pg)
    return sync_disclosure_daily(pro, days)


def backfill_fin_table(pro, table: str, codes: List[str],
                       limit: Optional[int] = None,
                       force_full: bool = False) -> int:
    """Backfill one financial table (stock by stock)"""
    api_name = FIN_TABLE_APIS[table]
    fields = table_fields(table)
    logger.info(f"回填 {table}: {len(codes)} 只股票, limit={limit}, force_full={force_full}")

    # Checkpoint: resume after the last ts_code (ignored when force_full=True)
    if not force_full:
        last_code = get_fin_progress(table)
        if last_code and last_code in codes:
            idx = codes.index(last_code)
            codes = codes[idx + 1:]
            logger.info(f"断点续传: 从 {last_code} 之后继续, 剩余 {len(codes)} 只")
        elif last_code:
            logger.info(f"断点续传: 上次进度 {last_code} 已不在白名单, 全量重跑")
    else:
        logger.info("强制全量回填 (忽略断点)")

    if limit:
        codes = codes[:limit]

    total = 0
    for i, ts_code in enumerate(codes, 1):
        rows = 0
        for attempt in range(1, 4):
            try:
                rows = _upsert_stock_fin(pro, table, api_name, fields, ts_code)
                break
            except PG_DOWN as e:
                logger.warning(f"{table} {ts_code} PG 断连 "
                               f"({attempt}/3): {str(e)[:80]}")
                time.sleep(10 * attempt)
        total += rows
        if rows:
            # Update progress after each stock (a crash resumes from that
            # stock); a disconnect only loses this write, the next stock's
            # retry self-heals
            try:
                set_fin_progress(table, ts_code, total)
            except PG_DOWN as e:
                logger.warning(f"{table} {ts_code} 水位线写入遇断连(忽略): {str(e)[:60]}")

        if i % 50 == 0:
            logger.info(f"  {table}: 已处理 {i}/{len(codes)} 只, 累计 {total} 行")

        time.sleep(API_SLEEP)

    logger.info(f"回填完成 {table}: 累计 {total} 行")
    return total


def run_financial(start_ts_code: Optional[str] = None,
                  tables: Optional[Iterable[str]] = None,
                  limit: Optional[int] = None,
                  force_full: bool = False) -> Dict[str, int]:
    """Run the full financial data backfill"""
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)

    # Initialize the fin schema
    with PgClient() as pg:
        init_fin_schema(pg)

    codes = get_whitelist_codes()
    if not codes:
        raise RuntimeError("白名单为空, 请先运行行情同步")

    if start_ts_code:
        try:
            start_idx = codes.index(start_ts_code)
            codes = codes[start_idx:]
        except ValueError:
            logger.warning(f"起始代码 {start_ts_code} 不在白名单, 全量开始")

    tables = set(tables or list(FIN_TABLE_APIS.keys()))
    result: Dict[str, int] = {}
    for t in tables:
        result[t] = backfill_fin_table(pro, t, codes, limit=limit,
                                       force_full=force_full)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tushare 财务数据回填 PostgreSQL")
    parser.add_argument("--tables", type=str,
                        default="income,balancesheet,cashflow,fina_indicator",
                        help="要同步的表 (income/balancesheet/cashflow/fina_indicator/dividend/fina_audit)")
    parser.add_argument("--start", type=str, default=None,
                        help="起始 ts_code (断点续传)")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理股票数 (测试用)")
    parser.add_argument("--full", action="store_true",
                        help="强制全量回填 (忽略断点)")
    parser.add_argument("--backfill-disclosure", action="store_true",
                        help="disclosure_date 按报告期整期批量回填 (不走逐股骨架)")
    args = parser.parse_args()

    if args.backfill_disclosure:
        import tushare as ts
        print("开始 disclosure_date 按报告期回填")
        n = backfill_disclosure_periods(ts.pro_api(get_config().tushare_token))
        print(f"disclosure_date 回填完成: {n} 行")
        raise SystemExit(0)

    tbls = [t.strip() for t in args.tables.split(",") if t.strip()]
    print(f"开始财务回填: tables={tbls}, limit={args.limit}, full={args.full}")
    result = run_financial(start_ts_code=args.start, tables=tbls,
                           limit=args.limit, force_full=args.full)
    print(f"财务回填完成: {result}")