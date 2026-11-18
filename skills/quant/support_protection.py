"""Confirmed native-60F higher lows for opt-in after-close risk reduction."""

from dataclasses import dataclass, replace
import math

import numpy as np
import pandas as pd

from skills.quant.prepare import swing_frame


@dataclass(frozen=True)
class SupportProtection:
    after_r: float = 0.5
    buffer_atr: float = 0.5

    def __post_init__(self):
        if (
            not math.isfinite(self.after_r)
            or not 0.5 <= self.after_r <= 2
            or not math.isfinite(self.buffer_atr)
            or not 0 <= self.buffer_atr <= 1
        ):
            raise ValueError("支撑保护启用进度须在0.5至2R、缓冲须在0至1ATR之间")


def attach_support(minute, stock):
    if "adjustment" in minute and not minute.adjustment.eq("none").all():
        raise ValueError("支撑计算只接收未复权原生分钟行情")
    frame, issue = swing_frame(minute, stock)
    if issue:
        raise ValueError(issue)
    low = frame.low.to_numpy(dtype=float, copy=True)
    times = pd.DatetimeIndex(frame.bar_end)
    n = len(frame)
    support = np.full(n, np.nan)
    origin = np.full(n, pd.NaT.value, dtype=np.int64)
    known = origin.copy()
    complete_days = frame.groupby(times.normalize()).bar_end.size().eq(4)
    valid_slots = {(10, 30), (11, 30), (14, 0), (15, 0)}
    valid = np.asarray(
        [
            bool(complete_days.get(t.normalize(), False))
            and (t.hour, t.minute) in valid_slots
            for t in times
        ]
    )
    previous, active, formed, confirmed = np.nan, np.nan, pd.NaT.value, pd.NaT.value
    for i in range(n):
        pivot = i - 2
        if pivot >= 2 and valid[pivot - 2 : i + 1].all():
            value = low[pivot]
            tolerance = 1e-10 * value
            if (
                value
                < min(low[pivot - 2 : pivot].min(), low[pivot + 1 : i + 1].min())
                - tolerance
            ):
                active = (
                    value
                    if math.isfinite(previous) and value > previous + tolerance
                    else np.nan
                )
                previous, formed, confirmed = value, times[pivot].value, times[i].value
        support[i], origin[i], known[i] = active, formed, confirmed
    # Only an actual same-day 15:00 bar can publish the daily decision state.
    closing = times.indexer_at_time("15:00")
    indexes = times[closing].get_indexer(stock.times)
    data = dict(stock.data)
    for key, values, empty in (
        ("confirmed_higher_low", support, np.nan),
        ("higher_low_origin", origin, pd.NaT.value),
        ("higher_low_known", known, pd.NaT.value),
    ):
        aligned = np.full(len(stock.times), empty, dtype=values.dtype)
        present = indexes >= 0
        aligned[present] = values[closing[indexes[present]]]
        data[key] = aligned
    return replace(stock, data=data)


def protected_stop(position, close, atr, support, origin, known, decision, ready, cfg):
    stop = position["stop"]
    if (
        not ready
        or not all(math.isfinite(v) for v in (close, atr, support))
        or atr <= 0
        or origin <= pd.Timestamp(position["entry_time"]).value
        or known > pd.Timestamp(decision).value
        or known < origin
        or close - position["first_price"] + 1e-8 * atr
        < cfg.after_r * position["initial_risk"]
    ):
        return stop
    return max(stop, min(support - cfg.buffer_atr * atr, close - 0.1 * atr))
