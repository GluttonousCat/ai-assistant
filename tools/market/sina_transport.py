"""Unadjusted Sina minute transport, matching AKShare's public endpoint.

Reference: AKShare stock_zh_a_minute (MIT). The SDK's auxiliary daily-price
adjustment requests are unnecessary for unadjusted bars and are omitted here.
No authentication, storage, retries or scheduler state belongs in this adapter.
"""
import json
import re

import pandas as pd
import requests

from tools.market.minute_history import _symbol, _boundary, normalize


def fetch_sina_history(code: str, start: str, end: str, period: int = 60) -> pd.DataFrame:
    symbol, market = _symbol(code)
    if period not in (5, 15, 30, 60):
        raise ValueError("unsupported minute period")
    response = requests.get(
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData",
        params={"symbol": f"{market}{symbol}", "scale": str(period), "ma": "no", "datalen": "1970"},
        timeout=(5, 15))
    response.raise_for_status()
    match = re.search(r"=\s*\((.*)\)\s*;?\s*$", response.text, re.S)
    if not match:
        raise ValueError("新浪分钟响应不是预期的 JSONP")
    payload = json.loads(match[1])
    if not isinstance(payload, list):
        raise ValueError("新浪分钟响应不是行情列表")
    frame = normalize(pd.DataFrame(payload), "akshare-sina", code, period, "none",
                      _boundary(start), _boundary(end, end=True))
    if not frame.empty:
        values = frame[["open", "high", "low", "close", "volume"]]
        import numpy as np
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            raise ValueError("分钟行情包含非有限数值")
        if ((frame.high < frame[["open", "close", "low"]].max(axis=1)) |
                (frame.low > frame[["open", "close", "high"]].min(axis=1)) |
                (frame.volume < 0)).any():
            raise ValueError("分钟行情OHLC或成交量不合法")
    return frame
