"""Repair native daily gaps and import actual limits. Never advances sync_meta."""

import argparse
from datetime import date
import time
import pandas as pd
from core.config import get_config
from core.logger import get_logger
from storage.schema.quant import ensure_quant_schema
from storage.schema.market import ensure_index_daily_schema
from storage.repositories import backtest as store
from tools.market.sync_index import sync_index
from tools.market.trend_data import scan_universe
import core.net  # noqa: F401

logger = get_logger(__name__)


def _retry(action):
    for attempt in range(3):
        try:
            return action()
        except Exception:
            if attempt == 2:
                raise
            logger.warning("回测输入单元失败，重试 %s/3", attempt + 1)
            time.sleep(attempt + 1)


def sync(start, end):
    import tushare as ts

    cfg = get_config()
    ensure_quant_schema()
    pro = ts.pro_api(cfg.tushare_token, timeout=20)
    members = scan_universe(end)
    codes = {r["ts_code"] for r in members}
    if not codes:
        raise ValueError("截止日之前没有指数成分名单")
    days, done, daily_days = store.input_dates(start, end)
    for day in days:
        stamp = day.strftime("%Y%m%d")
        if day not in daily_days:

            def repair():
                f = pro.daily(trade_date=stamp)
                if f is None or f.empty:
                    raise ValueError(f"{day}缺少真实日行情")
                f = f[f.ts_code.isin(codes)].copy()
                f["trade_date"] = pd.to_datetime(f.trade_date).dt.date
                return store.save_daily(f)

            count = _retry(repair)
            logger.info("修复原生日K %s %s行", day, count)
        if day in done:
            continue

        def limits():
            pages = []
            offset = 0
            while True:
                f = pro.stk_limit(
                    trade_date=stamp,
                    fields="ts_code,trade_date,up_limit,down_limit",
                    limit=5800,
                    offset=offset,
                )
                if f is None:
                    raise ValueError("涨跌停接口空响应")
                pages.append(f)
                if len(f) < 5800:
                    break
                offset += len(f)
                time.sleep(0.4)
            f = pd.concat(pages, ignore_index=True)
            if f.empty or f.duplicated(["ts_code", "trade_date"]).any():
                raise ValueError("涨跌停分页为空或重复，拒绝标记完成")
            f = f[f.ts_code.isin(codes)].copy()
            f["trade_date"] = pd.to_datetime(f.trade_date).dt.date
            return store.save_limits(f)

        count = _retry(limits)
        logger.info("真实涨跌停 %s %s行", day, count)
        time.sleep(0.4)
    for code in sorted(c for c in codes if c.endswith(".BJ")):

        def repair_bj():
            f = pro.daily(
                ts_code=code,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
            if f is None or f.empty:
                raise ValueError(f"{code}没有真实日K")
            f["trade_date"] = pd.to_datetime(f.trade_date).dt.date
            return store.save_daily(f)

        count = _retry(repair_bj)
        logger.info("BJ原生日K %s %s行", code, count)
        time.sleep(0.4)
    ensure_index_daily_schema()
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    for code in ("000300.SH", "000852.SH"):
        logger.info("基准 %s %s行", code, _retry(lambda: sync_index(pro, code, first, last)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default=date.today().isoformat())
    a = p.parse_args()
    sync(a.start, a.end)


if __name__ == "__main__":
    main()
