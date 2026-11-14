"""Ordered 60F advances, controlled retracements and tested reversal lows.

All pivots require two later bars before becoming known. No historical signal
is relabelled; v8's range-based rules remain frozen in swing.py.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from skills.trend.swing import SwingConfig

VERSION = "60f-ordered-structure-v9"


@dataclass(frozen=True)
class StructureConfig(SwingConfig):
    min_body_atr: float = 0.25
    min_volume_ratio: float = 0.8
    divergence_improvement_atr: float = 0.06
    pullback_depth_atr: float = 5
    min_impulse_atr: float = 4
    min_impulse_pct: float = 5
    max_retrace_fraction: float = 0.5
    min_pullback_bars: int = 2
    max_pullback_bars: int = 16
    contraction_ratio: float = 0.85

    def __post_init__(self):
        super().__post_init__()
        if not 0 < self.max_retrace_fraction < 1 or not 0 < self.contraction_ratio <= 1:
            raise ValueError("回撤/缩量比例不合法")
        if (
            type(self.min_pullback_bars) is not int
            or type(self.max_pullback_bars) is not int
            or not 1 <= self.min_pullback_bars <= self.max_pullback_bars
        ):
            raise ValueError("回调根数不合法")


def ordered_evidence(frame, cfg=StructureConfig()):
    """Persist one advance and its first/subsequent pullbacks in time order."""
    f = frame
    # Compare the same clock slot, avoiding morning-vs-afternoon seasonality.
    slot = f.bar_end.dt.strftime("%H:%M")
    baseline = f.groupby(slot).volume.transform(
        lambda x: x.shift().rolling(20, min_periods=5).mean()
    )
    relative = f.volume / baseline.replace(0, np.nan)
    prices = f.close.to_numpy()
    lows, highs = f.low.to_numpy(), f.high.to_numpy()
    atr = f.atr.to_numpy()
    rel = relative.to_numpy()
    fast, mid, slow = (f[f"ma{n}"].to_numpy() for n in (8, 13, 21))
    lows_confirmed = []
    leg, support = None, None
    records = []
    for i in range(len(f)):
        eps = 1e-8 * atr[i]
        j = i - 2
        pivot = (
            j >= 2
            and lows[j] < lows[j - 1]
            and lows[j] <= lows[j - 2]
            and lows[j] <= min(lows[j + 1 : i + 1])
        )
        if pivot and np.isfinite(atr[j]):
            lows_confirmed.append(j)
            # The support is a confirmed low, not a future swing annotation.
            support = (lows[j], j)
            if leg is None or (not leg["ready"] and lows[j] < leg["floor"]):
                leg = dict(
                    start=j,
                    floor=lows[j],
                    peak=prices[j],
                    peak_at=j,
                    ready=False,
                    pull=None,
                    pull_low=np.inf,
                    reference=np.nan,
                    volumes=[],
                    rounds=0,
                    waiting=False,
                    resumed=-1,
                )
        resumed = False
        a_valid = False
        stop_floor = np.nan
        if leg:
            if lows[i] < leg["floor"] - 0.25 * atr[i] or (
                not leg["ready"] and i - leg["start"] > 64
            ):
                leg = None
            else:
                if (
                    leg["pull"] is None
                    and not leg["waiting"]
                    and prices[i] > leg["peak"]
                ):
                    leg.update(peak=prices[i], peak_at=i)
                impulse = leg["peak"] - leg["floor"]
                if (
                    i - leg["start"] >= 4
                    and impulse >= cfg.min_impulse_atr * atr[i]
                    and impulse / leg["floor"] * 100 >= cfg.min_impulse_pct
                    and mid[i] - slow[i] > eps
                    and i >= 4
                    and slow[i] - slow[i - 4] > eps
                ):
                    leg["ready"] = True
                if (
                    leg["waiting"]
                    and i - leg["resumed"] >= 3
                    and prices[i] > leg["peak"] + eps
                ):
                    leg.update(waiting=False, peak=prices[i], peak_at=i)
                if (
                    leg["ready"]
                    and not leg["waiting"]
                    and leg["pull"] is None
                    and i > leg["peak_at"]
                    and prices[i] < prices[i - 1]
                    and leg["peak"] - lows[i] >= 0.75 * atr[i]
                ):
                    volume_slice = rel[
                        max(leg["start"], leg["peak_at"] - 3) : leg["peak_at"] + 1
                    ]
                    finite = volume_slice[np.isfinite(volume_slice)]
                    leg.update(
                        pull=i,
                        pull_low=lows[i],
                        volumes=[],
                        reference=float(finite.mean()) if len(finite) else np.nan,
                    )
                    leg["rounds"] += 1
                if leg["pull"] is not None:
                    leg["pull_low"] = min(leg["pull_low"], lows[i])
                    depth = leg["peak"] - leg["pull_low"]
                    age = i - leg["pull"] + 1
                    a_valid = (
                        age <= cfg.max_pullback_bars
                        and depth <= cfg.pullback_depth_atr * atr[i]
                        and depth <= cfg.max_retrace_fraction * impulse
                        and lows[i] >= slow[i] - atr[i]
                    )
                    if not a_valid:
                        leg = None
                    else:
                        volumes = np.asarray(leg["volumes"], dtype=float)
                        finite = volumes[np.isfinite(volumes)]
                        contracted = (
                            len(finite) >= cfg.min_pullback_bars
                            and finite.mean()
                            <= cfg.contraction_ratio * leg["reference"]
                        )
                        if (
                            age > cfg.min_pullback_bars
                            and contracted
                            and i >= 2
                            and prices[i] > max(highs[i - 2 : i]) + eps
                            and prices[i] >= fast[i]
                            and fast[i] - fast[i - 2] > eps
                        ):
                            resumed = True
                            stop_floor = leg["pull_low"]
                            leg.update(pull=None, waiting=True, resumed=i)
                        elif np.isfinite(rel[i]) and prices[i] <= prices[i-1] + eps:
                            leg["volumes"].append(rel[i])
                if leg and leg["ready"]:
                    a_valid = True if leg["waiting"] else a_valid
        # A second low must test a *previous* confirmed low after a real bounce.
        test = None
        if pivot and j >= 32:
            for p in reversed([p for p in lows_confirmed[:-1] if 5 <= j - p <= 40]):
                bounce = max(highs[p + 1 : j]) - lows[p]
                first_vol = rel[max(0, p - 1) : p + 2]
                test_vol = rel[max(0, j - 1) : j + 2]
                if (
                    np.isfinite(first_vol).all()
                    and np.isfinite(test_vol).all()
                    and bounce >= 2 * atr[j]
                    and lows[p] - 0.75 * atr[j] <= lows[j] <= lows[p] + 0.5 * atr[j]
                    and test_vol.mean() <= 0.9 * first_vol.mean()
                    and f.dif.iloc[p] < 0
                    and f.dif.iloc[j] - f.dif.iloc[p]
                    >= cfg.divergence_improvement_atr * atr[j]
                ):
                    test = dict(
                        confirmed=i, first=p, second=j, floor=min(lows[p], lows[j])
                    )
                    break
        records.append(
            dict(
                ordered_context=bool(a_valid),
                ordered_trigger=resumed,
                ordered_floor=stop_floor,
                pullback_round=leg["rounds"] if leg else 0,
                impulse_start=f.bar_end.iloc[leg["start"]] if leg else pd.NaT,
                impulse_peak=f.bar_end.iloc[leg["peak_at"]] if leg else pd.NaT,
                support=support[0] if support else np.nan,
                support_origin=f.bar_end.iloc[support[1]] if support else pd.NaT,
                test_event=test,
            )
        )
    result = pd.DataFrame(records, index=f.index)
    result["slot_volume_ratio"] = relative
    return result


def setups(frame, cfg=StructureConfig()):
    f = frame.copy()
    evidence = ordered_evidence(f, cfg)
    for key in evidence:
        f[key] = evidence[key]
    # Retain the last causal test for a short confirmation window.
    active, flags, floors = None, [], []
    for i, row in enumerate(f.itertuples()):
        if isinstance(row.test_event, dict):
            active = row.test_event
        if active and (
            i - active["confirmed"] > 8 or row.low < active["floor"] - 0.25 * row.atr
        ):
            active = None
        flags.append(active is not None)
        floors.append(active["floor"] if active else np.nan)
    f["b_context"] = flags
    f["reversal_floor"] = floors
    eps = 1e-8 * f.atr
    atr = f.atr.shift()
    body = (f.close - f.open) / atr
    location = (f.close - f.low) / (f.high - f.low).replace(0, np.nan)
    quality = (
        body.ge(cfg.min_body_atr)
        & location.ge(0.65)
        & f.slot_volume_ratio.ge(cfg.min_volume_ratio)
    )
    repair = f.close.ge(f.ma8) & f.ma8.diff(2).gt(eps)
    a = f.ordered_trigger & quality
    b = (
        f.b_context
        & repair
        & quality
        & f.close.gt(f.high.shift().rolling(2).max())
        & f["hist"].diff().gt(eps)
    )
    if cfg.family == "base_rebound":
        a[:] = False
    if cfg.family == "trend_pullback":
        b[:] = False
    floor = f.ordered_floor.where(a, f.reversal_floor)
    f["stop_price"] = np.minimum(floor - cfg.stop_buffer_atr * atr, f.close - atr)
    f["risk_pct"] = (f.close - f.stop_price) / f.close * 100
    f["pattern"] = np.where(a, "trend_pullback", np.where(b, "base_rebound", ""))
    f["a_context"] = f.ordered_context
    f["structure_valid"] = (f.ordered_context & f.close.gt(f.ma21 - f.atr)) | (
        f.b_context & f.close.gt(f.reversal_floor)
    )
    f["score"] = (
        60
        + 10 * body.clip(0, 2)
        + 10 * f.slot_volume_ratio.clip(0, 2)
        + 10 * (1 - f.risk_pct / cfg.max_risk_pct).clip(0, 1)
    ).fillna(0)
    raw = (
        (a | b)
        & f.risk_pct.between(cfg.min_risk_pct, cfg.max_risk_pct)
        & (f.close - f.ma13).le(2.5 * f.atr)
    )
    accepted, last = [], -cfg.cooldown_bars
    for i, trigger in enumerate(raw):
        allow = bool(trigger and i - last >= cfg.cooldown_bars)
        accepted.append(allow)
        if allow:
            last = i
    f["eligible"] = accepted
    f["structure_failed"] = (
        (f.close.lt(f.ma21 - eps) & f.ma13.diff(2).lt(-eps)).rolling(2).sum().eq(2)
    )
    return f
