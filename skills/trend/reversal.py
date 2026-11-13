"""Causal 60F reversal origins, retained across ordinary pullbacks.

A low price is not a safety guarantee. These gates describe a research setup,
not institutional flows or an executable intraday order.
"""

import numpy as np
import pandas as pd


def reversal_context(frame, max_age=12, cycle_reset=False):
    f = frame.copy()
    previous = f.close.shift()
    atr = f.atr.shift()
    floor = f.low.shift().rolling(16).min()
    peak = f.high.shift().rolling(32).max()
    span = f.high.shift().rolling(64).max() - f.low.shift().rolling(64).min()
    position = (previous - f.low.shift().rolling(64).min()) / span.replace(0, np.nan)
    decline = f.close.shift(17) - previous
    drift = decline.ge(2 * atr) & f.close.diff().shift().lt(0).rolling(16).mean().ge(
        0.5
    )
    pit = (f.high.shift().rolling(8).max() - previous).ge(3 * atr)
    background = (
        (drift | pit)
        & (peak - previous).ge(2 * atr)
        & (previous - floor).le(1.5 * atr)
        & position.le(0.45)
        & f.ma13.shift().lt(f.ma13.shift(5))
    ).fillna(False)
    location = (f.close - f.low) / (f.high - f.low).replace(0, np.nan)
    reclaim = f.close.gt(f.high.shift()) | f.close.ge(f.open.shift().rolling(2).max())
    volume = f.volume_ratio.ge(0.8) | f.volume.ge(f.volume.shift() * 1.15)
    seed = (
        background
        & (f.close - f.open).ge(0.5 * atr)
        & location.ge(0.65)
        & f.close.gt(previous)
        & reclaim
        & volume
    ).fillna(False)
    atr_values = atr.to_numpy(dtype=float)
    floor_values = floor.to_numpy(dtype=float)
    previous_values = previous.to_numpy(dtype=float)
    seed_values = seed.to_numpy(dtype=bool)
    pit_values = pit.to_numpy(dtype=bool)
    # Retire an old cycle only after a sustained new down-leg. A January low
    # cannot block an unrelated August reversal; brief pullbacks stay grouped.
    new_cycle = (
        (
            (f.close < f.ma21).shift().rolling(16).mean().ge(0.75)
            & (f.ma21.shift(9) - f.ma21.shift()).ge(atr)
            & (peak - previous).ge(4 * atr)
        )
        .fillna(False)
        .to_numpy(dtype=bool)
    )
    records, origin = [], None
    for i, r in enumerate(f.itertuples()):
        volatility = float(atr_values[i])
        # A local MA cross or a pause never erases the original start. A fresh
        # lower low invalidates that structure and permits a genuinely new pit.
        if origin and r.low < origin["floor"] - 0.25 * volatility:
            origin = None
        if (
            cycle_reset
            and origin
            and i - origin["index"] >= 64
            and new_cycle[i]
            and seed_values[i]
        ):
            origin = None
        ignition = bool(seed_values[i] and origin is None)
        if ignition:
            origin = dict(
                index=i,
                start=r.bar_end,
                floor=min(r.low, floor_values[i]),
                price=r.close,
                atr=volatility,
                bulls=0,
                kind="深坑反转" if pit_values[i] else "阴跌反转",
            )
        if origin:
            origin["bulls"] += int(r.close > r.open and r.close > previous_values[i])
            age = i - origin["index"] + 1
            gain = (r.close - origin["price"]) / origin["atr"]
            fresh = (
                age <= max_age
                and origin["bulls"] <= 4
                and gain <= 3
                and r.close >= origin["floor"]
                and r.close - r.ma21 <= 2 * r.atr
            )
            state = "早期反转观察" if fresh else "反转已过早期"
        else:
            age, gain, fresh, state = 0, np.nan, False, "无低位反转结构"
        records.append(
            dict(
                reversal_start=origin["start"] if origin else pd.NaT,
                reversal_floor=origin["floor"] if origin else np.nan,
                reversal_age=age,
                reversal_bulls=origin["bulls"] if origin else 0,
                reversal_gain_atr=gain,
                reversal_kind=origin["kind"] if origin else "",
                reversal_fresh=bool(fresh),
                reversal_ignition=ignition,
                reversal_state=state,
            )
        )
    return pd.concat([f, pd.DataFrame(records, index=f.index)], axis=1)
