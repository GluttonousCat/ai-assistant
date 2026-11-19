# -*- encoding: utf-8 -*-
"""
Index daily-bar sync (pulls core indexes from tushare index_daily into stock.index_daily)

Usage:
    python -m tools.market.sync_index            # full sync (from 2020)
    python -m tools.market.sync_index --start 20260101  # incremental
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from core.config import get_config
from core.logger import get_logger
import core.net  # noqa: F401
from storage.repositories.market import index_daily_gaps, save_index_daily
from storage.schema.market import ensure_index_daily_schema

logger = get_logger(__name__)

INDEXES = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "399006.SZ": "创业板指",
    "000016.SH": "上证50",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}
_PRICE_COLUMNS = ("open", "high", "low", "close", "pct_chg", "vol", "amount")


def sync_index(pro, code: str, start: date, end: date, *, required_days=()) -> int:
    frame = pro.index_daily(
        ts_code=code, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
    )
    if frame is None or frame.empty:
        raise ValueError("指数日K无数据")
    bars = frame.loc[:, ["trade_date", "ts_code", *_PRICE_COLUMNS]].copy()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], format="%Y%m%d").dt.date
    for column in _PRICE_COLUMNS:
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    if not (
        bars["ts_code"].eq(code).fillna(False).all()
        and bars["trade_date"].between(start, end).all()
        and bars["trade_date"].is_unique
        and bars["close"].between(0, float("inf"), inclusive="neither").all()
        and set(required_days).issubset(bars["trade_date"])
    ):
        raise ValueError("指数日K代码、日期或收盘价未通过校验")
    return int(save_index_daily(bars))


def _sync_ranges(pro, ranges):
    counts = {}
    for code, (start, end, required) in ranges.items():
        try:
            counts[code] = sync_index(pro, code, start, end, required_days=required)
            logger.info("%s(%s) 指数日K写入 %s 行", INDEXES[code], code, counts[code])
        except Exception as error:
            counts[code] = 0
            logger.warning("%s(%s) 指数日K同步失败: %s", INDEXES[code], code, type(error).__name__)
    return counts


def pending_index_days(target: date):
    return index_daily_gaps(INDEXES, target)


def _client():
    import tushare as ts
    return ts.pro_api(get_config().tushare_token, timeout=15)


def refresh_index_bars(target: date) -> dict:
    ensure_index_daily_schema()
    gaps = pending_index_days(target)
    ranges = {code: (days[0], days[-1], days) for code, days in gaps.items()}
    counts = _sync_ranges(_client(), ranges) if ranges else {}
    remaining = pending_index_days(target) if ranges else gaps
    return {
        "status": "partial" if remaining else "complete",
        "rows": counts,
        "missing": {code: [day.isoformat() for day in days] for code, days in remaining.items()},
    }


def sync_indexes(pro, start: str, end: str) -> dict:
    """Explicit history sync, returning the rows written per index."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("开始日期晚于结束日期")
    ensure_index_daily_schema()
    return _sync_ranges(pro, {code: (first, last, ()) for code in INDEXES})


def main():
    parser = argparse.ArgumentParser(description="指数日K同步")
    parser.add_argument("--start", type=str, default="20200101", help="开始日期 YYYYMMDD")
    parser.add_argument("--end", type=str, default=None, help="结束日期 YYYYMMDD (默认今天)")
    args = parser.parse_args()
    end = args.end or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    result = sync_indexes(_client(), args.start, end)
    print("同步完成:", result)


if __name__ == "__main__":
    main()
