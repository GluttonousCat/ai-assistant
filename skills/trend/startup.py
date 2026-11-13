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
    # Every precursor uses ONLY completed bars before the ignition candle.
    # A single rebound during an accelerating decline must not manufacture a base.
    old_floor = f.low.shift(5).rolling(8).min()
    support = f.low.shift().rolling(4).min() >= old_floor - 0.2 * prior_atr
    higher_low = (
        f.low.shift().rolling(2).min()
        > f.low.shift(3).rolling(2).min() + 0.1 * prior_atr
    )
    tr = pd.concat(
        [
            f.high - f.low,
            (f.high - f.close.shift()).abs(),
            (f.low - f.close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    contraction = tr.shift().rolling(4).mean() <= 0.9 * tr.shift(5).rolling(4).mean()
    selling = (f.open - f.close).clip(lower=0)
    old_selling = selling.shift(5).rolling(4).sum()
    selling_eases = (old_selling > 0.1 * prior_atr) & (
        selling.shift().rolling(4).sum() <= 0.7 * old_selling
    )
    signs = pd.DataFrame(
        {
            "低点抬高": higher_low,
            "波动收敛": contraction,
            "抛压减弱": selling_eases,
            "底部盘整": base,
        }
    ).fillna(False)
    prepared = support & signs.sum(axis=1).ge(cfg.precursor_min_signs)
    neckline = f.high.shift().rolling(4).max()
    f["precursor_ready"] = prepared.fillna(False)
    f["precursor_count"] = signs.sum(axis=1)
    f["precursor_support"] = support.fillna(False)
    evidence = pd.Series("", index=f.index)
    for label, mask in signs.items():
        evidence += np.where(mask, label + " / ", "")
    evidence = evidence.str.rstrip(" / ")
    body = f.close - f.open
    location = (f.close - f.low) / (f.high - f.low).replace(0, np.nan)
    ignition = (
        (body >= cfg.startup_body_atr * prior_atr)
        & (f.close > f.close.shift())
        & (f.close > f.high.shift())
    )
    ignition &= (location >= 0.65) & (f.volume_ratio >= cfg.startup_volume_ratio)
    ignition &= correction | (base & (f.close > f.high.shift().rolling(4).max()))
    if cfg.require_precursor:
        ignition &= prepared & (f.close > neckline)
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
