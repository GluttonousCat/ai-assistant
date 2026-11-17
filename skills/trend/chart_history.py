"""Five-year display prices layered over the snapshot's frozen strategy window."""
import json
from datetime import timedelta

import pandas as pd

from skills.trend.service import stock_detail
from tools.market.trend_data import adjusted_daily_bars, adjust_prices


def five_year_start(end):
    return (pd.Timestamp(str(end)[:10]) - pd.DateOffset(years=5)).date()


def stock_chart(code, start, end, config=None, timeframe="60F"):
    original = stock_detail(code, start, end, config, timeframe)
    if timeframe != "1D":
        return original
    begin = five_year_start(end)
    frame, issue = adjust_prices(adjusted_daily_bars(code, str(begin - timedelta(days=70)), end))
    if issue:
        return {**original, "history_years": 5, "history_issue": issue,
                "issue": f"近5年日K：{issue}", "data": [], "history_requested_start": str(begin)}
    for window in (8, 13, 21):
        frame[f"ma{window}"] = frame.close.rolling(window, min_periods=window).mean()
    frame = frame[frame.bar_end.astype(str).str[:10] >= str(begin)]
    fields = ["bar_end", "open", "high", "low", "close", "volume", "amount", "ma8", "ma13", "ma21"]
    prices = json.loads(frame[fields].to_json(orient="records", date_format="iso"))
    annotations = {str(row["bar_end"])[:10]: row for row in original["data"]}
    data = []
    for price in prices:
        row = {"eligible": False, "first_watch": False, "resume_watch": False,
               "swing_ignition": False, "reversal_ignition": False,
               **annotations.get(str(price["bar_end"])[:10], {}), **price}
        data.append(row)
    return {**original, "data": data, "history_years": 5,
            "history_requested_start": str(begin),
            "history_available_start": str(data[0]["bar_end"])[:10] if data else None,
            "chart_averages": {"type": "sma", "windows": [8, 13, 21]}}
