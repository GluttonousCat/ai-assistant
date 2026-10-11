"""
Tushare 财务数据 -> PostgreSQL 同步模块

功能:
- 全量回填: income(利润表) / balancesheet(资产负债表) / cashflow(现金流量表) /
  fina_indicator(财务指标) / dividend(分红送股)
- 四大财报表列结构与 Tushare 官方输出参数 1:1 (income 94 / balancesheet 158 /
  cashflow 97 / fina_indicator 167 字段; dividend 16 字段)
- 拉取字段不手工维护: 直接反射实表列(information_schema), 表结构即单一事实源
  (旧版 DDL 与请求字段双清单手工维护, 曾漂移出 10+16 个错名字段, API 静默不返回 -> 整列全 NULL)
- dividend 每日增量走 run_dividend_daily 按日批量三通道 (不逐股)
- 数据源: 单只股票拉取 (income 等接口 2000积分可用, income_vip 需5000积分)
- report_type=1 (合并报表, 最新)
- 断点续传: 按 ts_code 逐只, sync_meta 记录进度, 可反复执行

用法(命令行):
    python -m tools.market.sync_financial                   # 全量4表
    python -m tools.market.sync_financial --tables income   # 仅利润表
    python -m tools.market.sync_financial --limit 100       # 前100只测试
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

import pandas as pd
import psycopg2

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import init_fin_schema
import core.net  # noqa: F401  (强制直连, 绕过不稳定代理)

logger = get_logger(__name__)

API_SLEEP = 0.35  # 200次/分钟 -> 0.35s 间隔留余量

# PG 断连/服务端重启: 每股独立连接, 单股重试即重连 (坑表: 长批任务按工作单元续跑)
PG_DOWN = (psycopg2.OperationalError, psycopg2.InterfaceError)

# 各表对应的 Tushare 接口名 (表名 == 接口名); 拉取字段见 table_fields() 反射
FIN_TABLE_APIS = {
    "income": "income",
    "balancesheet": "balancesheet",
    "cashflow": "cashflow",
    "fina_indicator": "fina_indicator",
    "dividend": "dividend",
}

# upsert 冲突键: 缺省为三大财报表主键, 其他表按表覆盖
FIN_CONFLICT_KEYS = {
    "dividend": ["ts_code", "end_date", "ann_date", "div_proc"],
}

# 日期规范化: 基础三列之外按表追加 (dividend 有 7 个额外日期列)
DATE_NORM_EXTRA = {
    "dividend": ("record_date", "ex_date", "pay_date",
                 "div_listdate", "imp_ann_date", "base_date"),
}

_warned_dropped: set = set()


def table_fields(table: str) -> List[str]:
    """从实表反射拉取字段 (排除本地 created_at); 新增列无需改代码, 表结构即请求清单"""
    with PgClient() as pg:
        return [c["name"] for c in pg.get_columns("fin", table)
                if c["name"] != "created_at"]


def _filter_to_table(df: pd.DataFrame, table: str, table_cols: set) -> pd.DataFrame:
    """只保留表内列; API 返回表外列时告警(每组一次)。反射字段后正常应为空, 纯保险网。"""
    dropped = [c for c in df.columns if c not in table_cols]
    if dropped:
        key = (table, tuple(dropped))
        if key not in _warned_dropped:
            _warned_dropped.add(key)
            logger.warning(f"{table}: API 返回了表内不存在的列, 已丢弃: {dropped}")
    return df[[c for c in df.columns if c in table_cols]]

def get_whitelist_codes() -> List[str]:
    """从 PG stock_basic 读取白名单 ts_code 列表"""
    from tools.market.sync_tushare import get_whitelist
    codes = get_whitelist(use_cache=True, refresh_remote=False)
    return sorted(codes)


def get_fin_progress(table: str) -> Optional[str]:
    """已同步到的 ts_code (水位线, 续传用)"""
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
    """日期列 (YYYYMMDD int/str) 转 date 对象"""
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
    """拉取单只股票单个接口的历史财报数据"""
    try:
        df = getattr(pro, api_name)(
            ts_code=ts_code,
            start_date=start,
            fields=",".join(fields),
        )
        return df
    except Exception as e:
        logger.warning(f"  {api_name} {ts_code} 抓取失败: {e}")
        time.sleep(2)
        return pd.DataFrame()


def _upsert_stock_fin(pro, table: str, api_name: str, fields: List[str],
                      ts_code: str, start: str = "19900101") -> int:
    """单只股票 拉取+入库 工作单元 (返回入库行数); PG 断连由调用方按股重试"""
    df = fetch_stock_fin(pro, api_name, ts_code, fields, start=start)
    if df is None or df.empty:
        return 0
    for c in ("ann_date", "f_ann_date", "end_date") + DATE_NORM_EXTRA.get(table, ()):
        _normalize_date_col(df, c)
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
    增量拉取: 用 ann_date (公告日) 过滤, 只拉 start 之后公告的财报.
    财报公告是突发事件 (季报/年报/更正), 每日跑一次即可保持最新.
    返回新增行数. 单接口 200次/分钟限频, 与全量回填共用节流.
    """
    api_name = FIN_TABLE_APIS[table]
    fields = table_fields(table)
    total = 0
    for i, ts_code in enumerate(codes, 1):
        rows = 0
        for attempt in range(1, 4):
            try:
                rows = _upsert_stock_fin(pro, table, api_name, fields,
                                         ts_code, start=start)
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
    每日增量: 拉取最近 N 天公告的财报 (覆盖周末/节假日堆积的公告).
    逐股拉取限制在 API 限频内, 全市场 ~5000 只 x 4 表约 20 分钟.
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


# ---- dividend 每日增量: 按日批量三通道事件级拉取 (对比逐股 5000 次调用; 事件稀疏) ----

DIVIDEND_DATE_COLS = ("ann_date", "end_date", "record_date", "ex_date",
                      "pay_date", "div_listdate", "imp_ann_date", "base_date")


def _fetch_dividend_by_date(pro, param: str, d: str,
                            fields: List[str]) -> pd.DataFrame:
    """按单日参数拉全市场分红 (ann_date/imp_ann_date/ex_date 之一), API 失败重试 3 次"""
    for attempt in range(1, 4):
        try:
            return pro.dividend(**{param: d}, fields=",".join(fields))
        except Exception as e:
            logger.warning(f"dividend {param}={d} 抓取失败 ({attempt}/3): {e}")
            time.sleep(2 * attempt)
    return pd.DataFrame()


def sync_dividend_daily(pro, days: int = 2) -> int:
    """
    dividend 每日增量: 按日批量拉全市场, 三通道 (ann_date 新公告 / imp_ann_date 实施公告 /
    ex_date 除权除息兜底). upsert 幂等, 通道间与日间重复行安全.
    单日全市场分红事件 << 2000 行无翻页; 返回累计处理行数 (含幂等重复).
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
            # PK 列空值行 (理论上不应有, 防御 NOT NULL 违例中断整批)
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
    """调度入口: 建表兜底 + 按日增量"""
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)
    with PgClient() as pg:
        init_fin_schema(pg)
    return sync_dividend_daily(pro, days)


def backfill_fin_table(pro, table: str, codes: List[str],
                       limit: Optional[int] = None,
                       force_full: bool = False) -> int:
    """回填单张财务表 (逐股票)"""
    api_name = FIN_TABLE_APIS[table]
    fields = table_fields(table)
    logger.info(f"回填 {table}: {len(codes)} 只股票, limit={limit}, force_full={force_full}")

    # 断点: 从上次 ts_code 之后继续 (force_full=True 时忽略)
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
            # 每只股票更新进度 (崩溃可从该股票续)
            set_fin_progress(table, ts_code, total)

        if i % 50 == 0:
            logger.info(f"  {table}: 已处理 {i}/{len(codes)} 只, 累计 {total} 行")

        time.sleep(API_SLEEP)

    logger.info(f"回填完成 {table}: 累计 {total} 行")
    return total


def run_financial(start_ts_code: Optional[str] = None,
                  tables: Optional[Iterable[str]] = None,
                  limit: Optional[int] = None,
                  force_full: bool = False) -> Dict[str, int]:
    """执行财务数据全量回填"""
    import tushare as ts
    config = get_config()
    pro = ts.pro_api(config.tushare_token)

    # 初始化 fin schema
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
                        help="要同步的表 (income/balancesheet/cashflow/fina_indicator/dividend)")
    parser.add_argument("--start", type=str, default=None,
                        help="起始 ts_code (断点续传)")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理股票数 (测试用)")
    parser.add_argument("--full", action="store_true",
                        help="强制全量回填 (忽略断点)")
    args = parser.parse_args()

    tbls = [t.strip() for t in args.tables.split(",") if t.strip()]
    print(f"开始财务回填: tables={tbls}, limit={args.limit}, full={args.full}")
    result = run_financial(start_ts_code=args.start, tables=tbls,
                           limit=args.limit, force_full=args.full)
    print(f"财务回填完成: {result}")