"""Causal 60F candidate priorities: support distance and market-relative repair.

Keep entry definitions separate from the finite research of competing signals.
Ranking never uses subsequent returns or cross-sectional future availability.
"""

from dataclasses import replace

import numpy as np

from skills.quant.prepare import swing_frame
from skills.trend.swing import indicators


def attach_priority(minute, stock, market_return20):
    minute, issue = swing_frame(minute, stock)
    if issue:
        raise ValueError(issue)
    f = indicators(minute).set_index("bar_end")
    end = f.reindex(stock.times)
    atr_pct = end.atr / end.close
    beta = stock.data["market_beta"]
    residual = (f.close / f.close.shift(20) - 1).reindex(
        stock.times
    ) - beta * market_return20.reindex(stock.times)
    data = dict(stock.data)
    data["priority_relative"] = (residual / atr_pct).to_numpy(dtype=float, copy=True)
    data["priority_extension"] = ((end.close - end.ma13) / end.atr).to_numpy(
        dtype=float, copy=True
    )
    data["priority_slope"] = (
        (f.ma21.diff(4) / f.atr).reindex(stock.times).to_numpy(dtype=float, copy=True)
    )
    return replace(stock, data=data)


def prioritize(stock, style, max_extension):
    if style not in ("support", "relative", "blended"):
        raise ValueError("未知60F排序方法")
    signals = []
    for original in stock.signals:
        i = original["i"]
        extension = stock.data["priority_extension"][i]
        relative = stock.data["priority_relative"][i]
        slope = stock.data["priority_slope"][i]
        atr = stock.data["atr"][i]
        close = stock.data["adjusted_close"][i]
        risk = (close - original["stop"]) / atr if atr > 0 else np.nan
        if (
            not np.isfinite([extension, relative, slope, risk]).all()
            or extension > max_extension + 1e-8
        ):
            continue
        signal = dict(original)
        signal["original_score"] = signal["score"]
        signal["score"] = float(
            -risk
            if style == "support"
            else relative
            if style == "relative"
            else relative + slope - 0.25 * max(extension, 0) - 0.25 * risk
        )
        signals.append(signal)
    return replace(stock, data=dict(stock.data), signals=signals)
