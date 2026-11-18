"""Preplaced protective stops, resolved conservatively on native raw 60F bars.

This is an explicit execution scenario, not a fill at a hindsight daily low.
Stops are fixed before the session. Intrabar touch fills are approximations;
gap opens, price limits, volume caps and lot settlement still apply.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StopBar:
    end: pd.Timestamp
    open: float
    low: float
    volume: float
    resolution: str = "60F"
    close: float = float("nan")


@dataclass(frozen=True)
class IntradayProtection:
    after_r: float = 1
    minimum_profit_pct: float = 1

    def __post_init__(self):
        if not (
            math.isfinite(self.after_r)
            and 0.5 <= self.after_r <= 2
            and math.isfinite(self.minimum_profit_pct)
            and 0.5 <= self.minimum_profit_pct <= 3
        ):
            raise ValueError("盘中保本保护须在0.5至2R、0.5至3%浮盈后启用")


def completed_bar_protection(position, close, cost_floor, policy):
    if not math.isfinite(close) or close <= 0:
        raise ValueError("盘中保护缺少原生完成K线收盘价")
    progress = close - position["first_price"]
    eps = 1e-10 * close
    if (
        progress + eps < policy.after_r * position["initial_risk"]
        or progress / position["first_price"] * 100 + 1e-8 < policy.minimum_profit_pct
    ):
        return position["stop"]
    return max(position["stop"], min(cost_floor, close - eps))


def prepare_stop_bars(stock, minute, allow_daily_fallback=False):
    frame = minute.copy().sort_values("bar_end")
    frame["bar_end"] = pd.to_datetime(frame.bar_end)
    if frame.bar_end.duplicated().any() or not frame.adjustment.eq("none").all():
        raise ValueError("止损行情必须为唯一时间戳的原生未复权60F")
    numeric = frame[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    if (
        not np.isfinite(numeric).all()
        or (numeric[:, :4] <= 0).any()
        or (numeric[:, 4] < 0).any()
    ):
        raise ValueError("止损行情价格或成交量无效")
    if (
        (frame.high < frame[["open", "close", "low"]].max(axis=1))
        | (frame.low > frame[["open", "close", "high"]].min(axis=1))
    ).any():
        raise ValueError("止损行情OHLC无效")
    days = stock.times.normalize()
    frame["day"] = frame.bar_end.dt.normalize()
    frame = frame[frame.day.isin(days)].copy()
    frame["valid_time"] = (frame.bar_end.dt.hour * 60 + frame.bar_end.dt.minute).isin(
        [630, 690, 840, 900]
    )
    aggregate = (
        frame.groupby("day")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            count=("open", "size"),
            valid_time=("valid_time", "all"),
        )
        .reindex(days)
    )
    complete = aggregate["count"].eq(4) & aggregate.valid_time.eq(True)
    if not complete.all() and not allow_daily_fallback:
        day = days[np.flatnonzero(~complete.to_numpy())[0]].date()
        raise ValueError(f"{stock.code} {day}缺少完整原生60F，不能推测止损成交")
    raw = np.column_stack([stock.data[k] for k in ("open", "high", "low", "close")])
    consistent = (
        np.abs(aggregate[["open", "high", "low", "close"]].to_numpy() - raw) <= 0.011
    ).all(axis=1)
    if not consistent.all() and not allow_daily_fallback:
        day = days[np.flatnonzero(~consistent)[0]].date()
        raise ValueError(f"{stock.code} {day}日K与60F原始价格不一致")
    result = {day.date(): [] for day in days}
    for r in frame.itertuples():
        result[r.bar_end.date()].append(
            StopBar(
                r.bar_end,
                float(r.open),
                float(r.low),
                float(r.volume),
                close=float(r.close),
            )
        )
    valid = complete.to_numpy() & consistent
    for i in np.flatnonzero(~valid):
        day = days[i].date()
        result[day] = [
            StopBar(
                stock.times[i],
                float(stock.data["open"][i]),
                float(stock.data["low"][i]),
                float(stock.data["volume"][i]),
                "1D_fallback",
                float(stock.data["close"][i]),
            )
        ]
    return {day: tuple(bars) for day, bars in result.items()}


def stop_reference(bar, raw_stop, activated=False):
    if not math.isfinite(raw_stop) or raw_stop <= 0:
        raise ValueError("保护止损价必须为有限正数")
    if activated:
        return bar.open
    if bar.open <= raw_stop:
        return bar.open
    return raw_stop if bar.low <= raw_stop else None
