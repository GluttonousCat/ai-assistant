"""Causal index regimes and lagged market-relative evidence, never trade signals.

Two independent index series must have complete native sessions. Index daily
context is explicitly aggregated from completed 60F closes, without filling any
missing bar. Stock entry shapes remain exclusively 60F.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from skills.quant.prepare import MarketSeries, swing_frame
from skills.trend.swing import indicators

VERSION = "dual-index-context-v11"
MODES = ("any_fast", "any_trend", "both_trend", "avoid_both_down", "daily_trend")


@dataclass(frozen=True)
class MarketPolicy:
    entry_mode: str = "any_trend"
    guard_adds: bool = True
    relative_filter: bool = False
    soft_grace_days: int = 0

    def __post_init__(self):
        if self.entry_mode not in ("none", *MODES):
            raise ValueError("未知指数状态门槛")
        if type(self.guard_adds) is not bool or type(self.relative_filter) is not bool:
            raise ValueError("指数开关必须为布尔值")
        if type(self.soft_grace_days) is not int or self.soft_grace_days not in (0, 1):
            raise ValueError("系统性回撤观察只允许0或1个交易日")


def index_context(frames):
    if set(frames) != {"000300.SH", "000852.SH"}:
        raise ValueError("需要沪深300和中证1000原生60F")
    views = {}
    returns = []
    returns4 = []
    slots = {(10, 30), (11, 30), (14, 0), (15, 0)}
    for code, raw in frames.items():
        if raw.bar_end.duplicated().any():
            raise ValueError("指数重复时间")
        f = indicators(raw)
        numeric = f[["open", "high", "low", "close"]]
        if not np.isfinite(numeric).all().all() or (numeric <= 0).any().any():
            raise ValueError("指数价格无效")
        if (
            (f.high < f[["open", "close", "low"]].max(axis=1))
            | (f.low > f[["open", "close", "high"]].min(axis=1))
        ).any():
            raise ValueError("指数OHLC无效")
        f["day"] = f.bar_end.dt.normalize()
        f["valid_slot"] = [(t.hour, t.minute) in slots for t in f.bar_end]
        groups = f.groupby("day")
        complete = groups.bar_end.size().eq(4) & groups.valid_slot.all()
        eps = 1e-8 * f.atr
        f["fast"] = f.close.ge(f.ma8 - eps) & f.ma8.diff(2).gt(eps)
        f["trend"] = (
            f.close.ge(f.ma13 - eps)
            & (f.ma8 - f.ma13).gt(eps)
            & f.ma21.diff(4).ge(-0.1 * f.atr)
        )
        f["down"] = f.close.lt(f.ma21 - eps) & f.ma21.diff(4).lt(-eps)
        f["valid"] = f.day.map(complete) & f.ma21.notna() & f.atr.notna()
        closing = f[f.bar_end.dt.hour.eq(15) & f.bar_end.dt.minute.eq(0)].copy()
        close = closing.close
        daily13 = close.rolling(13).mean()
        closing["daily_trend"] = close.ge(daily13) & daily13.diff(3).gt(1e-10 * close)
        views[code] = closing.set_index("bar_end")[
            ["fast", "trend", "down", "valid", "daily_trend", "close"]
        ]
        price = f.set_index("bar_end").close
        returns.append(price.pct_change(fill_method=None).rename(code))
        returns4.append((price / price.shift(4) - 1).rename(code))
    left, right = [views[k] for k in sorted(views)]
    # Date-keyed inner join; absent sessions cannot reuse yesterday's state.
    joined = left.join(right, how="inner", lsuffix="_a", rsuffix="_b")
    ctx = pd.DataFrame(index=joined.index)
    ctx["valid"] = joined.valid_a & joined.valid_b
    ctx["any_fast"] = joined.fast_a | joined.fast_b
    ctx["any_trend"] = joined.trend_a | joined.trend_b
    ctx["both_trend"] = joined.trend_a & joined.trend_b
    ctx["avoid_both_down"] = ~(joined.down_a & joined.down_b)
    ctx["daily_trend"] = joined.daily_trend_a | joined.daily_trend_b
    r4 = pd.concat(returns4, axis=1)
    daily_r4 = r4.mean(axis=1, skipna=False).reindex(ctx.index)
    ctx["panic"] = (
        (daily_r4 <= -0.015) & r4.reindex(ctx.index).lt(0).all(axis=1) & ctx.valid
    )
    ctx["market_return_4"] = daily_r4
    minute_return = pd.concat(returns, axis=1).mean(axis=1, skipna=False)
    return ctx, minute_return, pd.concat(returns4, axis=1).mean(axis=1, skipna=False)


def apply_policy(stock, policy):
    data = dict(stock.data)
    valid = data["market_context_valid"]
    ready = (
        np.ones(len(stock.times), dtype=bool)
        if policy.entry_mode == "none"
        else data["market_mode_" + policy.entry_mode]
    )
    data["market_entry_allowed"] = valid & ready
    if policy.relative_filter:
        data["market_entry_allowed"] &= data["market_relative_positive"]
    data["market_add_allowed"] = (
        (valid & ready & ~data["market_systemic_weak"])
        if policy.guard_adds
        else np.ones(len(stock.times), dtype=bool)
    )
    return MarketSeries(
        stock.code,
        stock.name,
        stock.observed_at,
        stock.times.copy(),
        data,
        stock.signals,
    )


def attach_market(minute, stock, context, index_return, index_return4, policy=None):
    m, issue = swing_frame(minute, stock)
    if issue:
        raise ValueError(issue)
    f = indicators(m).set_index("bar_end")
    sr = f.close.pct_change(fill_method=None)
    mr = index_return.reindex(f.index)
    variance = mr.rolling(60, min_periods=40).var()
    beta = (
        sr.rolling(60, min_periods=40).cov(mr) / variance.where(variance > 1e-12)
    ).shift()
    beta = beta.clip(0, 3)
    excess = f.close / f.close.shift(4) - 1 - beta * index_return4.reindex(f.index)
    ctx = context.reindex(stock.times)
    valid = ctx.valid.fillna(False).to_numpy(dtype=bool)
    relative = excess.reindex(stock.times)
    data = dict(stock.data)
    for mode in MODES:
        data["market_mode_" + mode] = ctx[mode].fillna(False).to_numpy(dtype=bool)
    closing = f.reindex(stock.times)
    tolerance = 1e-8 * closing.atr / closing.close
    data["market_relative_positive"] = relative.ge(-tolerance).to_numpy(dtype=bool)
    data["market_resilient"] = (
        (relative.ge(-tolerance) & closing.close.ge(closing.ma21 - closing.atr))
        .fillna(False)
        .to_numpy(dtype=bool)
    )
    data["market_systemic_weak"] = ctx.panic.fillna(False).to_numpy(dtype=bool)
    data["market_context_valid"] = valid
    data["market_beta"] = beta.reindex(stock.times).to_numpy(dtype=float, copy=True)
    data["market_excess_pct"] = (relative * 100).to_numpy(dtype=float, copy=True)
    result = MarketSeries(
        stock.code,
        stock.name,
        stock.observed_at,
        stock.times.copy(),
        data,
        stock.signals,
    )
    return apply_policy(result, policy) if policy is not None else result
