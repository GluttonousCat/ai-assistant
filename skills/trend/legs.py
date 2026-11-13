"""Causal leg counter shared by legacy screening and the two setup families."""

import numpy as np
import pandas as pd


def track_leg(frame, cfg, ignition, anchors, prepared, evidence, neckline):
    f = frame.copy()
    prior_atr = f.atr.shift()
    falling = f.close.diff().lt(0).shift().rolling(3).sum().fillna(0)
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
                "prepared": bool(prepared.iloc[i]),
                "evidence": evidence.iloc[i],
                "neckline": neckline.iloc[i],
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
            values.append(
                (pd.NaT, 0, 0, np.nan, np.nan, 0.0, False, False, False, "", np.nan)
            )
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
                leg["prepared"],
                leg["evidence"],
                leg["neckline"],
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
        "startup_prepared",
        "startup_evidence",
        "startup_neckline",
    ]
    f[names] = pd.DataFrame(values, index=f.index, columns=names)
    return f
