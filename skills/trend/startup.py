"""Causal impulse tracking: first bullish bar, then advancing bullish bars 2–4.

A green candle alone is not a new leg. Re-arming requires an actual correction
or a compact base; small red candles do not reset the counter. No future pivots,
centered windows, or retrospective marking of a winning first candle are used.
"""

import numpy as np
import pandas as pd
from skills.trend.legs import track_leg


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
    return track_leg(f, cfg, ignition, anchors, prepared, evidence, neckline)
