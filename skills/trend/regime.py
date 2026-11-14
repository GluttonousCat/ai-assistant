"""60F market breadth and family-specific repair checks, without daily gates."""

import numpy as np
import pandas as pd

from skills.trend.swing import indicators


def closing_context(adjusted_minute):
    f = indicators(adjusted_minute)
    eps = 1e-8 * f.atr
    f["above"] = ((f.close - f.ma21).gt(eps) & f.ma21.diff(8).gt(eps)).astype(float)
    f.loc[f.ma21.isna(), "above"] = np.nan
    f["reversal_safe"] = (
        (f.close - f.ma13).gt(eps) & (f.ma8 - f.ma13).gt(eps) & f["hist"].gt(eps)
    )
    f["pullback_safe"] = (
        (f.close - f.ma13).gt(eps)
        & (f.ma8 - f.ma13).gt(eps)
        & (f.ma13 - f.ma21).gt(eps)
        & f.ma21.diff(8).gt(eps)
    )
    counts = f.groupby(f.bar_end.dt.normalize()).bar_end.transform("size")
    end = f.bar_end.dt.hour.eq(15) & f.bar_end.dt.minute.eq(0) & counts.eq(4)
    return f.loc[end, ["bar_end", "above", "reversal_safe", "pullback_safe"]].set_index(
        "bar_end"
    )


def breadth(contexts):
    """An IPO with only future observations cannot change older denominators."""
    panel = pd.DataFrame({code: f.above for code, f in contexts.items()})
    return pd.DataFrame(
        dict(fraction=panel.mean(axis=1), available=panel.notna().sum(axis=1))
    )


def allowed_signals(stock, context, market, threshold):
    signals = []
    for sig in stock.signals:
        when = stock.times[sig["i"]]
        if when not in context.index or when not in market.index:
            continue
        m = market.loc[when]
        if m.available < 100 or m.fraction < threshold:
            continue
        key = "pullback_safe" if sig["family"] == "trend_pullback" else "reversal_safe"
        if context.at[when, key]:
            signals.append(dict(sig))
    return signals
