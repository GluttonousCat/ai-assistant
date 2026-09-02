"""
Tushare 全量历史日K -> PostgreSQL 同步模块 (2000积分版)

功能:
- 股票池: 主板 + 创业板 + 科创板 (用 stock_basic, 排除北交所/B股/ST/退市)
- 全量回填: daily(日K) / adj_factor(复权因子) / daily_basic(每日估值)
- 断点续传: 基于 stock.sync_meta 水位线 + 表内实际数据, 可反复执行
- 交易日历: 用 trade_cal 同步到 PG, 供 K线分析使用

权限说明 (2000积分):
- daily        200次/分钟, 按 trade_date 一次取全市场
- adj_factor   200次/分钟
- daily_basic  200次/分钟

用法(命令行):
    python -m tools.market.sync_tushare --start 20000101
    python -m tools.market.sync_tushare --tables daily,adj_factor

【边界约束】本模块仅访问 Tushare API 与远程 PostgreSQL 的 SQL 数据读写,
不执行任何服务器级操作 (无 SSH / 无远端 Shell / 不修改服务器配置)。
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
import core.net  # noqa: F401  (强制直连, 绕过不稳定代理)

logger = get_logger(__name__)

# 板块白名单 (用 Tushare market 字段)
ALLOWED_MARKETS = ("主板", "创业板", "科创板")
# 排除前缀: 北交所(8/4)、B股(200/900)
EXCLUDE_SYMBOL_PREFIX = ("8", "4", "200", "900")

# 请求间隔(秒): 200次/分钟 -> 0.35s 间隔留余量
API_SLEEP = 0.35
# 每批多少交易日做一次批量写入 (单日约5000行, 10天一批约5万行)
BATCH_TRADE_DAYS = 10

# 模块级缓存
_WHITELIST_CACHE: Optional[set] = None
_TRADING_DAYS_CACHE: Optional[List[date]] = None


def _to_date(v) -> date:
    """归一化为 date 对象 (支持 str/YYYYMMDD, pd.Timestamp, datetime)"""
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
# 股票池 / 交易日
# ============================================================

def get_whitelist(pro=None, use_cache: bool = True,
                  refresh_remote: bool = True) -> set:
    """
    白名单 ts_code 集合 (主板+创业板+科创板, 剔除北交/B股/ST/退市)
    数据源优先级: 内存缓存 -> PG stock_basic -> Tushare 远程
    """
    global _WHITELIST_CACHE
    if use_cache and _WHITELIST_CACHE is not None:
        return _WHITELIST_CACHE

    # PG 缓存 (stock_basic 表本身即为白名单, 无需 status 过滤)
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
    # 板块过滤
    df = df[df["market"].isin(ALLOWED_MARKETS)]
    # 代码前缀过滤 (北交所 8/4, B股 200/900)
    df = df[~df["symbol"].str.startswith(EXCLUDE_SYMBOL_PREFIX)]
    # 名称过滤 ST/*ST/退
    df = df[~df["name"].str.contains("ST", case=False, na=False)]
    df = df[~df["name"].str.contains("退", na=False)]

    codes = set(df["ts_code"].tolist())
    logger.info(
        f"白名单(远程): {before} -> {len(codes)} "
        f"(主板/创业板/科创板, 剔除北交/B股/ST/退市)"
    )

    # 落地 PG
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
    """将 DataFrame 过滤到白名单"""
    if "ts_code" not in df.columns or df.empty:
        return df
    wl = get_whitelist(use_cache=True, refresh_remote=True)
    if wl:
        return df[df["ts_code"].isin(wl)]
    return df


def sync_trade_calendar(pro, start: date, end: date) -> int:
    """同步交易日历 (SSE) 到 PG"""
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
    # 空值填充 (pandas 新版 fillna 不接受 None 值)
    df = df.where(pd.notna(df), None)

    with PgClient() as pg:
        pg.upsert_df(df, "stock", "trade_calendar",
                     conflict_keys=["exchange", "cal_date"])
    global _TRADING_DAYS_CACHE
    _TRADING_DAYS_CACHE = None
    logger.info(f"交易日历同步完成: {len(df)} 行")
    return len(df)


def get_trading_days_in_range(pg: PgClient, start: date, end: date) -> List[date]:
    """从 PG trade_calendar 读取区间内交易日 (升序)"""
    rows = pg.fetch_all(
        f"SELECT cal_date FROM {T_TRADE_CALENDAR} "
        "WHERE is_open=1 AND cal_date BETWEEN %s AND %s ORDER BY cal_date",
        (start, end),
    )
    return [r["cal_date"] for r in rows]


# ============================================================
# 同步水位线
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
# 回填核心
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
    通用回填器: 逐交易日抓取 -> 批量写入 PG

    Args:
        table: 目标表名 ('daily' / 'adj_factor' / 'daily_basic')
        api_name: Tushare 接口函数名
        keys: 表主键列
        fields: 要写入的列 (Tushare 返回列的子集)
    """
    # 断点续传: 基于表内实际 MAX(trade_date) (比 sync_meta 更可靠)
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

    # 交易日列表
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

        # 保留需要的列
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

    # 循环结束后 flush 残留批次
    # (修复: 若最后一个交易日数据未入库(15-16点前), 之前用 i==len 判断
    #  flush 会被 continue 短路, 导致整批数据永不写入)
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
    """合并本批 DataFrame 并写入 PG, 返回写入行数"""
    if not dfs:
        return 0
    df = pd.concat(dfs, ignore_index=True)
    if df.empty:
        return 0
    with PgClient() as pg:
        pg.upsert_df(df, "stock", table, conflict_keys=list(keys))
    return len(df)


# ============================================================
# 完整流程
# ============================================================

# 各表配置: 表名 -> (Tushare接口名, 主键, 所需列)
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
    """Tushare -> PG 同步器"""

    def __init__(self):
        self.config = get_config()
        if not self.config.tushare_token:
            raise ValueError("未配置 TUSHARE_TOKEN (.env)")
        # Tushare API 直连, 绕过系统代理 (代理偶发断连导致回填崩溃)
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

        # 1. 初始化表结构
        with PgClient() as pg:
            init_schema(pg)

        # 2. 白名单 (首次会远程拉取落地 PG, 后续直接读 PG)
        codes = get_whitelist(pro, use_cache=False, refresh_remote=True)
        logger.info(f"白名单股票数: {len(codes)}")

        # 3. 交易日历
        sync_trade_calendar(pro, start, end)

        # 4. 回填各表
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