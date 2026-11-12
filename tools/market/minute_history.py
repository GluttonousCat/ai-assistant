"""Read-only historical minute adapters; network deadlines are enforced by the CLI worker.

Sina/EM expose rolling windows. Requested dates filter available rows and cannot
extend the supplier's history. Timestamps below are supplier bar-end labels in
Asia/Shanghai; no resampling, missing-bar filling or cross-source splicing occurs.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

SOURCES = ("akshare-sina", "akshare-em", "baostock")
ADJUSTMENTS = {"none": "", "forward": "qfq", "backward": "hfq"}
FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"


def _symbol(code: str) -> tuple[str, str]:
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", code.upper())
    if not match:
        raise ValueError("代码必须包含SH/SZ/BJ市场后缀，例如600000.SH或920001.BJ")
    return match[1], match[2].lower()


def _boundary(value: str | None, *, end: bool = False) -> pd.Timestamp | None:
    if not value:
        return None
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("日期不可为 NaT")
    if stamp.tzinfo:
        stamp = stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        stamp += pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return stamp


def normalize(frame: pd.DataFrame, source: str, code: str, period: int,
              adjustment: str, start: pd.Timestamp | None,
              end: pd.Timestamp | None) -> pd.DataFrame:
    """Normalize stock volume to shares and amount to CNY, without filling gaps."""
    columns = ["ts_code", "bar_end", "interval_minutes", "open", "high", "low",
               "close", "volume", "amount", "source", "adjustment"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    if source == "akshare-em":
        frame = frame.rename(columns={"时间": "bar_end", "开盘": "open", "最高": "high",
                                      "最低": "low", "收盘": "close", "成交量": "volume",
                                      "成交额": "amount"}).copy()
    else:
        frame = frame.rename(columns={"day": "bar_end", "time": "bar_end"}).copy()
    if source == "baostock":
        frame["bar_end"] = pd.to_datetime(frame["bar_end"], format="%Y%m%d%H%M%S%f", errors="raise")
    else:
        frame["bar_end"] = pd.to_datetime(frame["bar_end"], errors="raise")
    if frame["bar_end"].isna().any():
        raise ValueError("来源包含无效 K 线时间")
    for name in ["open", "high", "low", "close", "volume"]:
        frame[name] = pd.to_numeric(frame[name], errors="raise")
        if frame[name].isna().any():
            raise ValueError(f"来源字段 {name} 包含空值")
    frame["amount"] = pd.to_numeric(frame["amount"], errors="raise") if "amount" in frame else float("nan")
    if source == "akshare-em":
        frame["volume"] *= 100  # EM documents lots; Sina/BaoStock return shares.
    if start is not None:
        frame = frame[frame["bar_end"] >= start]
    if end is not None:
        frame = frame[frame["bar_end"] <= end]
    frame = frame.assign(ts_code=code.upper(), interval_minutes=period,
                         source=source, adjustment=adjustment)
    return frame[columns].sort_values("bar_end").drop_duplicates("bar_end", keep="last").reset_index(drop=True)


def fetch_minute_history(source: str, code: str, period: int, *, start: str | None = None,
                         end: str | None = None, adjustment: str = "none") -> pd.DataFrame:
    """Fetch one stock/period. Call in an isolated worker, never a request event loop.

    BaoStock's SDK holds global socket state and is not shared across threads.
    It requires an explicit start date here; dates are inclusive.
    """
    if source not in SOURCES or period not in (5, 15, 30, 60) or adjustment not in ADJUSTMENTS:
        raise ValueError("不支持的数据源、周期或复权类型")
    if source == "akshare-sina" and adjustment != "none":
        # Some AKShare versions return raw prices when adjustment lookup fails.
        # Do not label that fallback as adjusted data.
        raise ValueError("新浪适配器仅导出不复权数据；复权请使用东财或 BaoStock")
    symbol, market = _symbol(code)
    lower, upper = _boundary(start), _boundary(end, end=True)
    upper = upper if upper is not None else pd.Timestamp(datetime.now(ZoneInfo("Asia/Shanghai"))).tz_localize(None)
    if lower is not None and lower > upper:
        raise ValueError("开始时间不能晚于结束时间")
    if source == "baostock":
        if market == "bj":
            raise ValueError("BaoStock不支持BJ股票，请使用新浪源")
        if lower is None:
            raise ValueError("BaoStock 查询必须指定 --start")
        import baostock as bs
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock 登录失败 {login.error_code}: {login.error_msg}")
        try:
            result = bs.query_history_k_data_plus(
                f"{market}.{symbol}", FIELDS, start_date=str(lower.date()),
                end_date=str(upper.date()), frequency=str(period),
                adjustflag={"none": "3", "forward": "2", "backward": "1"}[adjustment])
            rows = []
            while result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
            # A paging/network failure must not silently return a partial success.
            if result.error_code != "0":
                raise RuntimeError(f"BaoStock 查询失败 {result.error_code}: {result.error_msg}")
            frame = pd.DataFrame(rows, columns=result.fields)
        finally:
            bs.logout()
    else:
        import akshare as ak
        if source == "akshare-sina":
            frame = ak.stock_zh_a_minute(symbol=f"{market}{symbol}", period=str(period),
                                       adjust=ADJUSTMENTS[adjustment])
        else:
            frame = ak.stock_zh_a_hist_min_em(
                symbol=symbol, period=str(period), adjust=ADJUSTMENTS[adjustment],
                start_date=str(lower) if lower is not None else "1990-01-01 00:00:00",
                end_date=str(upper))
    return normalize(frame, source, code, period, adjustment, lower, upper)
