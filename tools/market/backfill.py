# -*- encoding: utf-8 -*-
"""
Tushare 全量 A 股日K线回填脚本

- 只保留: 主板(60/00) + 创业板(30) + 科创板(688)
- 排除: 北交所(8/4/92开头) 和 ST 股票
- 数据: 从各股票上市日期(list_date)到最近一个交易日, 逐股分批拉取
- 写入: MySQL 表 daily_data (ts_code, trade_date, open, high, low, close,
        pre_close, change, pct_chg, vol, amount), 按 ts_code+trade_date 去重(UPSERT)
- 进度: 实时打印进度条 / 可断点续跑(已完成的 ts_code 自动跳过)
- 用法:
    python -m tools.market.backfill                     # 默认全量
    python -m tools.market.backfill --days 30           # 只回填最近30个交易日
    python -m tools.market.backfill --start 20240101    # 只回填2024-01-01之后
    python -m tools.market.backfill --limit 50          # 只处理前50只(调试)
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from storage.mysql import MysqlHelper
from tools.market.tushare_client import TushareClient

logger = get_logger(__name__)

TABLE = "daily_data"


# ======================================================================
# 股票过滤: 主板 / 创业板 / 科创板, 排除北交所与 ST
# ======================================================================
def is_allowed_ts_code(ts_code: str, name: str = "", list_status: str = "") -> bool:
    """
    判断股票是否属于允许范围 (主板/创业板/科创板, 非北交所, 非ST)
    规则:
    - 主板:  60xxxx.SH / 00xxxx.SZ / 000xxx.SZ(B股) / 200xxx.SZ(mainboard B)
    - 创业板: 30xxxx.SZ
    - 科创板: 688xxx.SH / 689xxx.SH(CDR)
    - 排除: 8xxxxx / 4xxxxx / 92xxxx(BJ北交所), 43/83/87(BJ), 03x(科创板旁支),
            ST/*ST 名称
    """
    if not ts_code or "." not in ts_code:
        return False

    symbol, market = ts_code.split(".")
    if market == "SH":
        return (symbol.startswith("60") or symbol.startswith("688")
                or symbol.startswith("689"))
    elif market == "SZ":
        return (symbol.startswith("00") or symbol.startswith("30")
                or symbol.startswith("200"))  # 深圳主板含 B股200
    return False


def is_st_name(name: str) -> bool:
    """判断是否为 ST / *ST 股票"""
    if not name:
        return False
    n = name.strip().upper()
    return n.startswith("ST") or "*ST" in n or "ST" in n.split(" ")[0]


def build_whitelist(client: TushareClient) -> pd.DataFrame:
    """
    获取允许的股票列表 (已过滤板块与ST)
    返回: ts_code, symbol, name, market, list_date, delist_date
    """
    df = client.pro.stock_basic(
        exchange="", list_status="L",  # 只取上市状态正常
        fields="ts_code,symbol,name,market,list_date,delist_date",
    )
    if df is None or df.empty:
        logger.error("stock_basic 返回空, 请检查 TUSHARE_TOKEN 权限(需 pro 积分)")
        return pd.DataFrame()

    before = len(df)
    df = df[df.apply(lambda r: is_allowed_ts_code(r["ts_code"], r["name"]),
                     axis=1)]
    df = df[~df["name"].apply(is_st_name)]
    dropped = before - len(df)
    logger.info(f"股票池: 原始 {before} 只 -> 允许 {len(df)} 只 (剔除 {dropped} 只)")
    return df.reset_index(drop=True)


# ======================================================================
# 数据抓取与入库
# ======================================================================
def fetch_stock_daily(client: TushareClient, ts_code: str,
                      start_date: str, end_date: str) -> pd.DataFrame:
    """拉取单只股票区间日线, 返回标准化 DataFrame"""
    df = client.pro.daily(ts_code=ts_code, start_date=start_date,
                          end_date=end_date)
    if df is None or df.empty:
        return pd.DataFrame()
    # 统一列名与顺序 (Tushare: vol 单位手, amount 单位千元)
    cols = ["ts_code", "trade_date", "open", "high", "low", "close",
            "pre_close", "change", "pct_chg", "vol", "amount"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols].copy()


def get_existing_codes(db: MysqlHelper) -> set:
    """查询已入库的 ts_code (用于断点续跑)"""
    try:
        rows = db.fetch_all(f"SELECT DISTINCT ts_code FROM `{TABLE}`")
        return {r["ts_code"] for r in rows}
    except Exception:
        return set()


def upsert_daily(db: MysqlHelper, df: pd.DataFrame,
                 batch_size: int = 2000) -> int:
    """批量 UPSERT 日K数据"""
    if df is None or df.empty:
        return 0
    return db.insert_df(df, table=TABLE, batch_size=batch_size, replace=True)


def create_daily_table(db: MysqlHelper):
    """建表 (若不存在), 主键为 ts_code+trade_date 保证去重"""
    db.create_table(
        TABLE,
        """
        ts_code   VARCHAR(16)  NOT NULL COMMENT '股票代码',
        trade_date DATE        NOT NULL COMMENT '交易日期',
        open      DECIMAL(10,3) COMMENT '开盘价',
        high      DECIMAL(10,3) COMMENT '最高价',
        low       DECIMAL(10,3) COMMENT '最低价',
        close     DECIMAL(10,3) COMMENT '收盘价',
        pre_close DECIMAL(10,3) COMMENT '昨收',
        change    DECIMAL(10,3) COMMENT '涨跌额',
        pct_chg   DECIMAL(10,3) COMMENT '涨跌幅%',
        vol       BIGINT        COMMENT '成交量(手)',
        amount    DECIMAL(18,2) COMMENT '成交额(千元)',
        PRIMARY KEY (ts_code, trade_date),
        KEY idx_trade_date (trade_date)
        """,
    )


# ======================================================================
# 主流程
# ======================================================================
def backfill(args) -> int:
    """执行回填, 返回成功处理的股票数"""
    config = get_config()
    client = TushareClient()

    # 参数校验
    start_date = None
    if getattr(args, "start", None):
        start_date = args.start

    end_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    if getattr(args, "end", None):
        end_date = args.end
    logger.info(f"回填区间: {start_date or '(最早上市日)'} ~ {end_date}")

    # MySQL
    db = MysqlHelper.from_config()
    create_daily_table(db)
    existing = get_existing_codes(db)

    # 股票池
    whitelist = build_whitelist(client)
    if whitelist.empty:
        return 0
    if getattr(args, "limit", 0) and 0 < args.limit < len(whitelist):
        whitelist = whitelist.head(args.limit)
        logger.info(f"调试模式: 仅处理前 {args.limit} 只")

    # 日期下界: 若指定 start 则用; 否则取最早上市日期
    default_start = None
    if start_date is None:
        list_dates = whitelist["list_date"].replace("", pd.NaT).dropna()
        if len(list_dates):
            default_start = list_dates.min().strftime("%Y%m%d")
            logger.info(f"最早上市日期: {default_start}")
        else:
            default_start = "20000101"

    processed, skipped = 0, 0
    total = len(whitelist)
    t0 = time.time()

    for idx, row in whitelist.iterrows():
        ts_code = row["ts_code"]
        symbol = ts_code.split(".")[0]

        # 断点续跑
        if ts_code in existing:
            skipped += 1
            if processed % 200 == 0:
                logger.info(f"[{processed + skipped}/{total}] ...")
            continue

        st = default_start
        if start_date:
            st = start_date
        # 该股上市日期若晚于下界, 从上市日拉
        if row.get("list_date") and str(row["list_date"]) not in ("", "None"):
            try:
                list_dt = datetime.strptime(str(row["list_date"]), "%Y%m%d")
                if list_dt.strftime("%Y%m%d") > st:
                    st = list_dt.strftime("%Y%m%d")
            except Exception:
                pass

        if st > end_date:
            skipped += 1
            continue

        try:
            df = fetch_stock_daily(client, ts_code, st, end_date)
        except Exception as e:
            msg = str(e)
            if "权限" in msg or "积分" in msg:
                logger.error(f"积分不足, 停止回填: {e}")
                break
            logger.warning(f"[{symbol}] 拉取失败: {e}")
            time.sleep(1)
            continue

        n = upsert_daily(db, df)
        processed += 1
        existing.add(ts_code)

        if processed % 20 == 0 or processed == total:
            el = time.time() - t0
            speed = processed / max(el / 60, 1e-9)
            eta = (total - processed - skipped) / max(speed, 1e-9)
            logger.info(
                f"[{processed + skipped}/{total}] 完成 {processed} 只 "
                f"(跳过 {skipped}) | {symbol} 写入 {n} 条 | "
                f"速度 {speed:.1f} 只/min | 预计剩余 {eta:.0f} 分钟")

        # 控制请求频率 (Tushare 限频 500次/min 高积分档, 保守 0.2s/request)
        time.sleep(0.2)

    elapsed = time.time() - t0
    logger.info(f"回填完成: 处理 {processed} 只, 跳过 {skipped} 只, "
                f"耗时 {elapsed / 60:.1f} 分钟")
    return processed


def parse_args():
    parser = argparse.ArgumentParser(description="Tushare 日K全量回填")
    parser.add_argument("--start", type=str, default=None,
                        help="开始日期 YYYYMMDD (默认: 从最早上市日期)")
    parser.add_argument("--end", type=str, default=None,
                        help="结束日期 YYYYMMDD (默认: 昨天)")
    parser.add_argument("--days", type=int, default=None,
                        help="回填最近N个交易日 (会用N天前的日期作为起点)")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理前N只股票 (调试用)")
    return parser.parse_args()


def main():
    args = parse_args()

    # --days 便捷参数: 覆写 start
    if args.days:
        from tools.kline.calendar import get_offset_trading_day
        base = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            offset_date = get_offset_trading_day(base, -(args.days - 1))
            args.start = offset_date.strftime("%Y%m%d")
            logger.info(f"--days {args.days} => 起点 {args.start}")
        except Exception as e:
            logger.warning(f"交易日推算失败, 用日历日期回退: {e}")
            # 粗退: 直接用日历天数
            args.start = (datetime.now() - timedelta(days=args.days * 1.5)
                          ).strftime("%Y%m%d")

    backfill(args)


if __name__ == "__main__":
    main()