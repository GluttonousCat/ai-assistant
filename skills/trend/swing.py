"""Causal 60F divergence/pullback setups with after-work execution planning.

Daily candles supply execution prices and liquidity only. Pivot lows become
known two bars later; a historical pivot never receives a backdated signal.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from skills.trend.mtf import features, MtfConfig

VERSION = "60f-divergence-pullback-eod-v8"


@dataclass(frozen=True)
class SwingConfig:
    family: str = "both"
    min_body_atr: float = 0.2
    min_volume_ratio: float = 0.8
    divergence_improvement_atr: float = 0.04
    pullback_depth_atr: float = 3
    stop_buffer_atr: float = 0.35
    max_risk_pct: float = 4
    min_risk_pct: float = 0.8
    min_daily_amount: float = 30_000_000
    cooldown_bars: int = 12

    def __post_init__(self):
        if self.family not in ("both", "base_rebound", "trend_pullback"):
            raise ValueError("未知60F机会类型")
        for key, value in vars(self).items():
            if key == "family":
                continue
            if not math.isfinite(value) or value <= 0:
                raise ValueError("60F阈值必须为有限正数")
        if not self.min_risk_pct < self.max_risk_pct <= 5:
            raise ValueError("风险区间不合法，上限不得超过5%")
        if type(self.cooldown_bars) is not int:
            raise ValueError("冷却期必须为整数")


def indicators(minute):
    f = features(minute, MtfConfig())
    f["dif"] = (
        f.close.ewm(span=12, adjust=False).mean()
        - f.close.ewm(span=26, adjust=False).mean()
    )
    f["dea"] = f.dif.ewm(span=9, adjust=False).mean()
    f["hist"] = f.dif - f.dea
    return f


def divergence_evidence(f, improvement):
    """Compare confirmed local lows, using only the prefix visible at each bar."""
    lows = f.low.to_numpy()
    dif = f.dif.to_numpy()
    atr = f.atr.to_numpy()
    peaks = f.high.shift().rolling(32).max().to_numpy()
    pivots, active, rows = [], None, []
    for i in range(len(f)):
        j = i - 2
        if (
            j >= 32
            and lows[j] < lows[j - 1]
            and lows[j] <= lows[j - 2]
            and lows[j] <= min(lows[j + 1 : i + 1])
        ):
            candidates = [p for p in pivots if 5 <= j - p <= 40]
            for p in reversed(candidates):
                if (
                    lows[j] <= lows[p] + 0.15 * atr[j]
                    and dif[p] < 0
                    and dif[j] < 0
                    and dif[j] - dif[p] >= improvement * atr[j]
                    and peaks[j] - lows[j] >= 2 * atr[j]
                ):
                    active = dict(start=i, pivot=j, floor=lows[j], first=p)
                    break
            pivots = [p for p in pivots if j - p <= 40] + [j]
        if active and (
            i - active["start"] > 8 or lows[i] < active["floor"] - 0.5 * atr[i]
        ):
            active = None
        rows.append(
            dict(
                divergence=active is not None,
                divergence_floor=active["floor"] if active else np.nan,
                divergence_origin=f.bar_end.iloc[active["pivot"]] if active else pd.NaT,
                divergence_confirmed=f.bar_end.iloc[active["start"]]
                if active
                else pd.NaT,
            )
        )
    return pd.DataFrame(rows, index=f.index)


def setups(f, cfg=SwingConfig()):
    f = f.copy()
    evidence = divergence_evidence(f, cfg.divergence_improvement_atr)
    for key in evidence:
        f[key] = evidence[key]
    atr = f.atr.shift()
    eps = 1e-8 * f.atr
    location = (f.close - f.low) / (f.high - f.low).replace(0, np.nan)
    body = (f.close - f.open) / atr
    turn = (f.close > f.high.shift()) | (
        (f.close > f.close.shift()) & (f["hist"] > f["hist"].shift()) & body.ge(0.35)
    )
    quality = (
        body.ge(cfg.min_body_atr)
        & location.ge(0.65)
        & turn
        & f.volume_ratio.ge(cfg.min_volume_ratio)
    )
    repair = f.close.ge(f.ma8) & f.ma8.diff(2).gt(eps)
    f["b_context"] = f.divergence
    prior_up = (
        ((f.ma13 > f.ma21) & f.ma21.diff(4).gt(eps))
        .shift(3)
        .rolling(12)
        .max()
        .fillna(0)
        .gt(0)
    )
    peak = f.high.shift().rolling(16).max()
    floor = f.low.shift().rolling(16).min()
    touch = (
        (f.low.le(f.ma13 + 0.3 * f.atr) | f.low.le(f.ma21 + 0.3 * f.atr))
        .shift()
        .rolling(5)
        .max()
        .fillna(0)
        .gt(0)
    )
    depth = peak - f.low.rolling(5).min()
    f["a_context"] = (
        prior_up
        & touch
        & (peak - floor).ge(2 * atr)
        & depth.between(0.5 * atr, cfg.pullback_depth_atr * atr)
        & f.close.ge(f.ma21 - 0.5 * atr)
        & f.ma21.diff(4).ge(-0.1 * atr)
    )
    a = f.a_context & repair & quality & f.close.gt(f.high.shift())
    b = f.b_context & repair & quality
    if cfg.family == "base_rebound":
        a[:] = False
    elif cfg.family == "trend_pullback":
        b[:] = False
    local_floor = f.low.rolling(3).min()
    stop_floor = local_floor.where(~b, np.minimum(local_floor, f.divergence_floor))
    f["stop_price"] = np.minimum(stop_floor - cfg.stop_buffer_atr * atr, f.close - atr)
    f["risk_pct"] = (f.close - f.stop_price) / f.close * 100
    f["pattern"] = np.where(a, "trend_pullback", np.where(b, "base_rebound", ""))
    f["score"] = (
        60
        + 15 * body.clip(0, 2)
        + 10 * f.volume_ratio.clip(0, 2)
        + 10 * (1 - f.risk_pct / cfg.max_risk_pct).clip(0, 1)
    ).fillna(0)
    raw = (
        (a | b)
        & f.risk_pct.between(cfg.min_risk_pct, cfg.max_risk_pct)
        & (f.close - f.ma13).le(2.5 * f.atr)
        & f.ma21.notna()
    )
    last, accepted = -cfg.cooldown_bars, []
    for i, trigger in enumerate(raw):
        allow = bool(trigger and i - last >= cfg.cooldown_bars)
        accepted.append(allow)
        if allow:
            last = i
    f["eligible"] = accepted
    return f


def execution_plans(evaluated, daily, cfg=SwingConfig()):
    """Today's 60F triggers survive to today's close, then order next session.

    Missing/duplicated native slots invalidate the day's plan. No daily candle,
    daily MA or daily momentum predicate is introduced here.
    """
    f = evaluated.copy()
    f["day"] = f.bar_end.dt.normalize()
    liquidity = daily.set_index(daily.bar_end.dt.normalize()).amount.rolling(20).mean()
    slots = {(10, 30), (11, 30), (14, 0), (15, 0)}
    f["valid_slot"] = [(t.hour, t.minute) in slots for t in f.bar_end]
    groups = f.groupby("day")
    complete = (
        groups.bar_end.size().eq(4)
        & groups.bar_end.nunique().eq(4)
        & groups.valid_slot.all()
    )
    closing = f[f.bar_end.dt.hour.eq(15) & f.bar_end.dt.minute.eq(0)]
    ends = closing[["day", "bar_end", "close", "low", "ma8", "ma13", "atr"]]
    candidates = f[f.eligible].groupby("day", sort=False).tail(1)
    signals = candidates[["day", "bar_end", "pattern", "stop_price", "score"]].rename(
        columns={"bar_end": "signal_60f"}
    )
    plans = signals.merge(ends, on="day", how="inner", validate="one_to_one")
    plans["risk_pct"] = (plans.close - plans.stop_price) / plans.close * 100
    valid = (
        plans.day.map(complete).fillna(False)
        & plans.day.map(liquidity).ge(cfg.min_daily_amount)
        & plans.risk_pct.between(cfg.min_risk_pct, cfg.max_risk_pct)
        & plans.close.ge(plans.ma8)
        & plans.low.gt(plans.stop_price)
        & (plans.close - plans.ma13).le(2.5 * plans.atr)
    )
    return plans.loc[
        valid, ["bar_end", "signal_60f", "pattern", "stop_price", "score", "risk_pct"]
    ].reset_index(drop=True)
