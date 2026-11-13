"""Retain a parent price swing across short pullbacks and setup-family changes.

This classifies an existing local signal; it never relaxes an entry/risk gate.
A background engulfing candle is not automatically a tradable first candle.
"""

import numpy as np
import pandas as pd


def add_swing_context(f, cfg):
    f = f.copy()
    atr = f.atr.shift()
    bearish = f.close < f.open
    red_top = (
        f.open.where(bearish)
        .shift()
        .rolling(cfg.swing_engulf_bars, min_periods=1)
        .max()
    )
    red_bottom = (
        f.close.where(bearish)
        .shift()
        .rolling(cfg.swing_engulf_bars, min_periods=1)
        .min()
    )
    red_count = bearish.shift().rolling(cfg.swing_engulf_bars).sum()
    location = (f.close - f.low) / (f.high - f.low).replace(0, np.nan)
    engulf = (
        (f.close > f.open)
        & (f.close - f.open >= cfg.swing_body_atr * atr)
        & (red_count >= 2)
        & (f.close >= red_top)
        & (f.open <= red_bottom + 0.25 * atr)
        & (location >= 0.65)
    ).fillna(False)
    anchors = f.low.shift().rolling(cfg.swing_engulf_bars).min().to_numpy()
    # track_leg freezes prepared/evidence even if a seed's initial risk gates fail.
    starts = {
        key: pd.to_datetime(f[f"{key}_startup_start"]).tolist() for key in ["a", "b"]
    }
    times = pd.to_datetime(f.bar_end).tolist()
    lows, highs, closes, slows = (
        f[c].to_numpy() for c in ["low", "high", "close", "ema_slow"]
    )
    volatility_values = atr.to_numpy()
    engulf_values = engulf.to_numpy()
    phase_memory = {}
    parent = None
    output = []
    for i in range(len(f)):
        when = times[i]
        lo, hi, cl = lows[i], highs[i], closes[i]
        volatility = volatility_values[i]
        if parent is not None:
            broken_floor = lo < parent["low"] - 0.2 * volatility
            broken_trend = (
                cl < slows[i] - cfg.swing_slow_buffer_atr * volatility
            ) and (parent["peak"] - cl >= cfg.swing_break_atr * volatility)
            if broken_floor or broken_trend:
                parent = None
        prior = parent.copy() if parent is not None else None
        local_new = {
            key: pd.notna(starts[key][i]) and starts[key][i] == when
            for key in ["a", "b"]
        }
        event = False
        if (
            parent is None
            and (engulf_values[i] or any(local_new.values()))
            and np.isfinite(anchors[i])
            and np.isfinite(volatility)
        ):
            parent = {
                "start": when,
                "low": min(lo, anchors[i]),
                "peak": hi,
                "reason": "包阴强阳（背景）"
                if engulf_values[i]
                else "前次结构转强（背景）",
            }
            event = True
        if parent is not None:
            parent["peak"] = max(parent["peak"], hi)
        values = {}
        for key in ["a", "b"]:
            start = starts[key][i]
            identity = (key, start)
            if pd.notna(start) and identity not in phase_memory:
                # A is an intact trend's pullback by definition, even if the
                # original launch lies before the available/recognized history.
                phase = (
                    "pullback_resume"
                    if key == "a" or prior is not None
                    else "initial_reversal"
                )
                context = prior or parent
                phase_memory[identity] = (
                    phase,
                    context["start"] if context else pd.NaT,
                    context["low"] if context else np.nan,
                    context["reason"] if context else "",
                )
            values[key] = phase_memory.get(identity, ("", pd.NaT, np.nan, ""))
        output.append(
            (event, parent["start"] if parent else pd.NaT, *values["a"], *values["b"])
        )
    columns = [
        "swing_ignition",
        "background_swing_start",
        "a_setup_phase",
        "a_swing_start",
        "a_swing_low",
        "a_swing_reason",
        "b_setup_phase",
        "b_swing_start",
        "b_swing_low",
        "b_swing_reason",
    ]
    f[columns] = pd.DataFrame(output, index=f.index, columns=columns)
    use_a = f.pattern.eq("trend_pullback")
    for name in ["setup_phase", "swing_start", "swing_low", "swing_reason"]:
        f[name] = f[f"a_{name}"].where(use_a, f[f"b_{name}"])
    local_watch = f.first_watch.copy()
    f["resume_watch"] = local_watch & f.setup_phase.eq("pullback_resume")
    f["first_watch"] = local_watch & ~f.resume_watch
    return f
