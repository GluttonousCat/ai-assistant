"""Dashboard composition: cached scan plus one stock's causal chart indicators."""

import json
import pandas as pd

from tools.market.trend_data import adjusted_bars, adjust_prices, load_dashboard
from skills.trend.strategy import TrendConfig, indicators


def dashboard(run_id=None, include_rows=True):
    return load_dashboard(run_id, include_rows)


def stock_detail(code: str, start: str, end: str, config: dict | None = None):
    frame = adjusted_bars(code, start, end)
    frame, issue = adjust_prices(frame)
    if frame.empty:
        return {"data": [], "issue": issue, "price_basis": "raw", "code": code}
    if issue:
        for c in ("open", "high", "low", "close", "volume", "amount"):
            frame[c] = pd.to_numeric(frame[c], errors="coerce")
    data = indicators(frame, TrendConfig(**(config or {})))
    cols = [
        "bar_end",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "ema_fast",
        "ema_slow",
        "atr",
        "adx",
        "efficiency",
    ]
    clean = (
        data[cols]
        .tail(1000)
        .astype(object)
        .where(pd.notna(data[cols].tail(1000)), None)
    )
    return {
        "code": code,
        "data": json.loads(clean.to_json(orient="records", date_format="iso")),
        "issue": issue,
        "price_basis": "raw" if issue else "forward_asof",
        "as_of": end,
        "time_zone": "Asia/Shanghai",
        "volume_unit": "share",
        "amount_unit": "CNY",
    }
