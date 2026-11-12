"""Tushare adjustment-factor transport, independent of stock_basic whitelists."""

import time
import numpy as np
import pandas as pd
from core.config import get_config
import core.net  # noqa: F401 direct supplier connection, matching existing market transport


def fetch_adjustments(*, day=None, code=None, start="2026-01-01", end="2026-12-31"):
    import tushare as ts

    pro = ts.pro_api(get_config().tushare_token)
    params = (
        {"trade_date": str(day).replace("-", "")}
        if day
        else {
            "ts_code": code,
            "start_date": start.replace("-", ""),
            "end_date": end.replace("-", ""),
        }
    )
    parts = []
    offset = 0
    while True:
        for attempt in range(3):
            try:
                frame = pro.adj_factor(
                    **params,
                    fields="ts_code,trade_date,adj_factor",
                    limit=6000,
                    offset=offset,
                )
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        if frame is None or frame.empty:
            break
        frame["trade_date"] = pd.to_datetime(frame.trade_date, format="%Y%m%d").dt.date
        frame["adj_factor"] = pd.to_numeric(frame.adj_factor, errors="raise")
        if not np.isfinite(frame.adj_factor).all() or (frame.adj_factor <= 0).any():
            raise ValueError("供应商复权因子无效")
        if day and not frame.trade_date.eq(pd.Timestamp(day).date()).all():
            raise ValueError("供应商返回日期与请求不一致")
        if code and not frame.ts_code.eq(code).all():
            raise ValueError("供应商返回代码与请求不一致")
        parts.append(frame)
        if len(frame) < 6000:
            break
        offset += 6000
        time.sleep(0.4)
    return (
        pd.concat(parts, ignore_index=True).drop_duplicates(["ts_code", "trade_date"])
        if parts
        else pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])
    )
