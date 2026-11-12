"""Causal impulse tracking: first bullish bar, then advancing bullish bars 2–4.

A green candle alone is not a new leg. Re-arming requires an actual correction
or a compact base; small red candles do not reset the counter. No future pivots,
centered windows, or retrospective marking of a winning first candle are used.
"""

import numpy as np
import pandas as pd


def add_startup_features(frame: pd.DataFrame, cfg) -> pd.DataFrame:
    f = frame.copy()
    prior_atr = f.atr.shift()
    prior_high = f.high.shift().rolling(20).max()
    base_high = f.high.shift().rolling(8).max()
    base_low = f.low.shift().rolling(8).min()
    path = f.close.diff().abs().shift().rolling(8).sum()
    base_er = (f.close.shift() - f.close.shift(9)).abs() / path.replace(0, np.nan)
    drawdown = prior_high - f.close.shift()
    falling = f.close.diff().lt(0).shift().rolling(3).sum().fillna(0)
    base = ((base_high - base_low) <= cfg.startup_base_atr * prior_atr) & (
        base_er <= 0.35
    )
    correction = (drawdown >= cfg.startup_reset_atr * prior_atr) & (
        (f.close.shift() < f.ema_fast.shift()) | (falling >= 2)
    )
    body = f.close - f.open
    location = (f.close - f.low) / (f.high - f.low).replace(0, np.nan)
    ignition = (
        (body >= cfg.startup_body_atr * prior_atr)
        & (f.close > f.close.shift())
        & (f.close > f.high.shift())
    )
    ignition &= (location >= 0.65) & (f.volume_ratio >= cfg.startup_volume_ratio)
    ignition &= correction | (base & (f.close > f.high.shift().rolling(4).max()))
    anchors = f.low.shift().rolling(8).min()
    values = []
    leg = None
    # ndarray scalars avoid per-row pandas indexing overhead in full-pool scans.
    rows = zip(
        f.bar_end,
        f.open,
        f.high,
        f.low,
        f.close,
        prior_atr,
        f.ema_fast,
        ignition.fillna(False),
        anchors,
        falling,
    )
    for i, (when, op, hi, lo, cl, atr, fast, seed, anchor, down_count) in enumerate(
        rows
    ):
        advancing = False
        if leg is not None:
            retraced = leg["peak"] - cl >= cfg.startup_reset_atr * atr
            reset = retraced and (cl < fast or down_count >= 2)
            if lo < leg["low"] - 1e-10 or reset:
                leg = None
        if leg is None and seed and np.isfinite(anchor):
            leg = {
                "start": when,
                "index": i,
                "low": min(lo, anchor),
                "origin": float(f.close.iloc[i - 1]),
                "last_bull": cl,
                "count": 1,
                "peak": hi,
                "path": abs(cl - f.close.iloc[i - 1]),
                "last_close": cl,
                "pause": 0,
                "expired": False,
            }
            advancing = True
        elif leg is not None:
            leg["path"] += abs(cl - leg["last_close"])
            leg["last_close"] = cl
            leg["peak"] = max(leg["peak"], hi)
            advancing = (
                cl > op and cl > leg["last_bull"] + 1e-10 and cl - op >= 0.1 * atr
            )
            if advancing:
                leg["count"] += 1
                leg["last_bull"] = cl
                leg["pause"] = 0
            else:
                leg["pause"] += 1
            if leg["pause"] > cfg.startup_pause_bars:
                leg["expired"] = True
        if leg is None:
            values.append((pd.NaT, 0, 0, np.nan, np.nan, 0.0, False, False))
            continue
        age = i - leg["index"] + 1
        efficiency = (
            max(0.0, min(1.0, (cl - leg["origin"]) / leg["path"]))
            if leg["path"]
            else 0.0
        )
        early = age <= cfg.startup_max_bars and not leg["expired"]
        values.append(
            (
                leg["start"],
                leg["count"],
                age,
                leg["low"],
                leg["origin"],
                efficiency,
                advancing,
                early,
            )
        )
    names = [
        "startup_start",
        "startup_count",
        "startup_age",
        "startup_low",
        "startup_origin",
        "startup_efficiency",
        "startup_advancing",
        "startup_early",
    ]
    f[names] = pd.DataFrame(values, index=f.index, columns=names)
    return f
