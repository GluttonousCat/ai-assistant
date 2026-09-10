# -*- encoding: utf-8 -*-
"""
十大股东同步 (Tushare top10_holders; 单接口含流通比例/变动/类型)

按股增量: 已有 max(end_date) 之后续拉 (tushare 按股全期一次返回, 增量只省入库量).
限频: 每股一调用, 间隔 RATE 秒 + 失败重试; 积分不足时接口会报错并跳过该股.

用法:
    python -m tools.market.sync_holders --index 000300.SH          # 沪深300
    python -m tools.market.sync_holders --stock 中际旭创           # 单只
    python -m tools.market.sync_holders --index 000300.SH --years 5
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from typing import Dict, List, Optional

from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import DDL_TOP10_HOLDERS

logger = get_logger(__name__)

RATE = 0.35            # 每股调用间隔 (秒)
RETRIES = 3


def _load_stocks(index_code: str = "", stock: str = "") -> List[Dict[str, str]]:
    if stock:
        from skills.fin_query.skill import lookup_ts_code
        ts = lookup_ts_code(stock.strip())
        return [{"ts_code": ts, "name": stock}] if ts else []
    if index_code:
        import tushare as ts
        from core.config import get_config
        from datetime import timedelta
        pro = ts.pro_api(get_config().tushare_token)
        start = (date.today() - timedelta(days=95)).strftime("%Y%m%d")
        df = pro.index_weight(index_code=index_code, start_date=start,
                              end_date=date.today().strftime("%Y%m%d"))
        latest = df["trade_date"].max()
        codes = sorted(df[df["trade_date"] == latest]["con_code"].unique())
        with PgClient() as pg:
            rows = pg.fetch_all("SELECT ts_code, name FROM stock.stock_basic "
                                "WHERE ts_code = ANY(%s)", (codes,))
        by = {r["ts_code"]: r["name"] for r in rows}
        return [{"ts_code": c, "name": by.get(c, "")} for c in codes if c in by]
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT ts_code, name FROM stock.stock_basic "
            "WHERE delist_date IS NULL OR delist_date > current_date "
            "ORDER BY ts_code")
    return rows


def _since(pg, ts_code: str, years: int) -> str:
    """增量起点: 已有 max(end_date) 次日; 无数据则 years 年前"""
    row = pg.fetch_one("SELECT MAX(end_date) d FROM stock.top10_holders "
                       "WHERE ts_code=%s", (ts_code,))
    if row and row["d"]:
        return row["d"].strftime("%Y%m%d")
    y = date.today().year - years
    return f"{y}0101"


def sync(ts_code: str, name: str = "", years: int = 5) -> Dict[str, int]:
    import tushare as ts
    from core.config import get_config
    pro = ts.pro_api(get_config().tushare_token)
    stats = {"saved": 0, "failed": 0}
    with PgClient() as pg:
        pg.execute(DDL_TOP10_HOLDERS)          # 幂等建表
        pg.conn.commit()
        start = _since(pg, ts_code, years)
        df = None
        for attempt in range(1, RETRIES + 1):
            try:
                df = pro.top10_holders(ts_code=ts_code, start_date=start,
                                       end_date=date.today().strftime("%Y%m%d"))
                break
            except Exception as e:  # noqa: BLE001 限频/积分
                logger.warning(f"top10 {ts_code} 失败 ({attempt}/{RETRIES}): "
                               f"{str(e)[:80]}")
                time.sleep(min(60, 10 * attempt))
        if df is None:
            stats["failed"] = 1
            logger.warning(f"top10 {ts_code} {name}: 接口重试耗尽, 跳过 "
                           f"(增量起点不丢失, 重跑自动补)")
            return stats
        if df.empty:
            return stats
        for _, r in df.iterrows():
            pg.execute(
                """
                INSERT INTO stock.top10_holders
                    (ts_code, ann_date, end_date, holder_name, hold_amount,
                     hold_ratio, hold_float_ratio, hold_change, holder_type)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (ts_code, end_date, holder_name, holder_type)
                DO UPDATE SET hold_amount=EXCLUDED.hold_amount,
                    hold_ratio=EXCLUDED.hold_ratio,
                    hold_float_ratio=EXCLUDED.hold_float_ratio,
                    hold_change=EXCLUDED.hold_change,
                    ann_date=EXCLUDED.ann_date, updated_at=now()
                """,
                (ts_code, r.get("ann_date") or None, r.get("end_date"),
                 r.get("holder_name"), r.get("hold_amount"),
                 r.get("hold_ratio"), r.get("hold_float_ratio"),
                 r.get("hold_change"), r.get("holder_type")))
            stats["saved"] += 1
        pg.conn.commit()
    logger.info(f"top10 {ts_code} {name}: +{stats['saved']} 行 (since {start})")
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description="十大股东同步")
    p.add_argument("--index", default="", help="指数成分 (如 000300.SH)")
    p.add_argument("--stock", default="", help="单只 (名或代码)")
    p.add_argument("--years", type=int, default=5, help="无数据时回看年数")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    if not args.index and not args.stock:
        p.error("--index 或 --stock 至少一个 (全市场慎用, 限频)")

    stocks = _load_stocks(args.index, args.stock)
    if args.limit:
        stocks = stocks[:args.limit]
    logger.info(f"十大股东同步: {len(stocks)} 只")
    total = {"saved": 0, "failed": 0}
    import psycopg2
    pg_down = (psycopg2.OperationalError, psycopg2.InterfaceError)
    for i, s in enumerate(stocks, 1):
        st = {"saved": 0, "failed": 1}
        for attempt in range(1, RETRIES + 1):
            try:
                st = sync(s["ts_code"], s.get("name", ""), args.years)
                break
            except pg_down as e:   # PG 服务端断连: 每股独立连接, 重试即重连
                logger.warning(f"top10 {s['ts_code']} PG 断连 "
                               f"({attempt}/{RETRIES}): {str(e)[:80]}")
                time.sleep(10 * attempt)
        for k in total:
            total[k] += st.get(k, 0)
        if i % 50 == 0:
            logger.info(f"进度 {i}/{len(stocks)}")
        time.sleep(RATE)
    print(f"[TOP10] {total}")
    return 0 if total["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
