"""Causal price/volume proxies for spring tests, LPS and supply warnings.

These bounded events are not a complete Wyckoff phase classifier. ATR measures
spread, while relative effort compares only earlier bars at the same clock slot.
No price moving-average position or MACD is an entry/management predicate.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

VERSION = "wyckoff-price-volume-60f-v33"


@dataclass(frozen=True)
class WyckoffConfig:
    contraction_ratio: float = 0.8
    stop_buffer_atr: float = 0.35
    maximum_risk_pct: float = 4
    minimum_score: float = 72
    minimum_daily_amount: float = 30_000_000
    demand_hold_atr: float = 0.25

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in vars(self).values()):
            raise ValueError("量价策略参数须为有限正数")
        if (
            not 0.5 <= self.contraction_ratio <= 1
            or not 0.1 <= self.stop_buffer_atr <= 1
            or not 0 < self.maximum_risk_pct <= 4
            or not 0 <= self.minimum_score <= 100
            or not 0 < self.demand_hold_atr <= 1
        ):
            raise ValueError("缩量、支撑缓冲、风险或评分范围无效")


def features(minute):
    f = minute.copy().sort_values("bar_end").reset_index(drop=True)
    f["bar_end"] = pd.to_datetime(f.bar_end)
    if f.empty or f.bar_end.duplicated().any():
        raise ValueError("量价策略缺少唯一时间戳的行情")
    if (
        not f.bar_end.dt.strftime("%H:%M")
        .isin(["10:30", "11:30", "14:00", "15:00"])
        .all()
    ):
        raise ValueError("量价策略只接收原生60F完成时间")
    values = f[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    if (
        not np.isfinite(values).all()
        or (values[:, :4] <= 0).any()
        or (values[:, 4] < 0).any()
        or (f.high < f[["open", "close", "low"]].max(axis=1)).any()
        or (f.low > f[["open", "close", "high"]].min(axis=1)).any()
    ):
        raise ValueError("量价策略OHLC或成交量无效")
    previous = f.close.shift()
    tr = pd.concat(
        [f.high - f.low, (f.high - previous).abs(), (f.low - previous).abs()], axis=1
    ).max(axis=1)
    f["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().shift()
    slot = f.bar_end.dt.strftime("%H:%M")
    baseline = f.groupby(slot).volume.transform(
        lambda x: x.shift().rolling(20, min_periods=5).mean()
    )
    f["effort"] = f.volume / baseline.replace(0, np.nan)
    f["spread"] = f.high - f.low
    f["close_location"] = (f.close - f.low) / f.spread.replace(0, np.nan)
    f["floor20"] = f.low.shift().rolling(20).min()
    f["ceiling20"] = f.high.shift().rolling(20).max()
    f["ceiling8"] = f.high.shift().rolling(8).max()
    return f


def events(frame, cfg=WyckoffConfig()):
    f = frame.copy()
    columns = [
        "open",
        "high",
        "low",
        "close",
        "atr",
        "effort",
        "spread",
        "close_location",
        "floor20",
        "ceiling20",
        "ceiling8",
    ]
    a = {k: f[k].to_numpy(dtype=float, copy=True) for k in columns}
    times = pd.DatetimeIndex(f.bar_end)
    spring = leg = warning = None
    support = np.nan
    formed = known = pd.NaT
    rows = []
    for i, when in enumerate(times):
        o, h, l, c, atr, v, spread, loc, floor, ceiling, high8 = (
            a[k][i] for k in columns
        )
        kind = ""
        contraction = np.nan
        event_floor = np.nan
        event_origin = pd.NaT
        sos = False
        ready = (
            all(math.isfinite(x) for x in (atr, v, loc, floor, ceiling, high8))
            and atr > 0
            and f.volume.iloc[i] > 0
        )
        eps = 1e-8 * atr if math.isfinite(atr) else 0
        prev = a["close"][i - 1] if i else c
        if ready:
            if warning and (i - warning["i"] > 4 or c > warning["high"] + eps):
                warning = None
            if (
                warning
                and i > warning["i"]
                and c < warning["low"] - 0.1 * atr
                and v >= 1
            ):
                warning["confirmed"] = True
            high_context = ceiling - floor >= 3 * atr and h >= ceiling - 0.5 * atr
            if high_context and v >= 1.8 and loc <= 0.4 and c - prev <= 0.1 * atr:
                if warning is None or not warning["confirmed"]:
                    warning = dict(i=i, low=l, high=h, confirmed=False)
            if spring and (i - spring["i"] > 12 or l < spring["low"] - 0.15 * atr):
                spring = None
            if (
                (spring is None or l < spring["low"] - eps)
                and ceiling - floor >= 3 * atr
                and ceiling / floor - 1 >= 0.03
                and l < floor - eps
                and c > floor + eps
                and floor - l <= 0.8 * atr
                and loc >= 0.55
            ):
                spring = dict(i=i, low=l, effort=v, spread=spread, test=None)
            if spring and i - spring["i"] >= 2:
                test = spring["test"]
                if test and l < test["low"] - eps:
                    spring["test"] = None
                    test = None
                if (
                    test
                    and i - test["i"] >= 2
                    and c > test["high"] + 0.05 * atr
                    and v >= 1
                    and loc >= 0.65
                ):
                    kind = "spring_test"
                    event_floor = test["low"]
                    event_origin = times[test["i"]]
                    contraction = test["ratio"]
                    spring = None
                elif (
                    l >= spring["low"] - eps
                    and l - spring["low"] <= 0.75 * atr
                    and v <= cfg.contraction_ratio * spring["effort"]
                    and spread <= 0.85 * spring["spread"]
                    and loc >= 0.5
                ):
                    if test is None:
                        spring["test"] = dict(
                            i=i, low=l, high=h, ratio=v / spring["effort"]
                        )
            sos = (
                c > high8 + 0.1 * atr
                and c > o
                and spread >= 0.8 * atr
                and v >= 1.2
                and loc >= 0.65
            )
            if leg and (c < leg["floor"] - 0.25 * atr or i - leg["i"] > 40):
                leg = None
            if sos and leg is None:
                start = max(0, i - 8)
                leg = dict(
                    i=i,
                    floor=float(a["low"][start : i + 1].min()),
                    peak=h,
                    efforts=[v],
                    spreads=[spread],
                    pull=None,
                )
            if leg:
                if leg["pull"] is None:
                    if c < prev and leg["peak"] - l >= 0.5 * atr:
                        leg["pull"] = dict(i=i, low=l, at=i, efforts=[], spreads=[])
                    else:
                        leg["peak"] = max(leg["peak"], h)
                        if c > prev:
                            leg["efforts"] = (leg["efforts"] + [v])[-12:]
                            leg["spreads"] = (leg["spreads"] + [spread])[-12:]
                pull = leg["pull"]
                if pull:
                    if l < pull["low"]:
                        pull.update(low=l, at=i)
                    if c < prev:
                        pull["efforts"].append(v)
                        pull["spreads"].append(spread)
                    impulse = leg["peak"] - leg["floor"]
                    if (
                        i - pull["i"] > 16
                        or impulse < 2 * atr
                        or impulse / leg["floor"] < 0.03
                        or leg["peak"] - pull["low"] > 0.6 * impulse
                    ):
                        leg = None
                    elif len(pull["efforts"]) >= 2 and i - pull["at"] >= 2:
                        ratio = float(
                            np.mean(pull["efforts"]) / np.mean(leg["efforts"])
                        )
                        narrow = float(
                            np.mean(pull["spreads"]) / np.mean(leg["spreads"])
                        )
                        if (
                            ratio <= cfg.contraction_ratio
                            and narrow <= 0.85
                            and c > a["high"][i - 1] + 0.05 * atr
                            and c > prev
                            and v >= 1
                            and loc >= 0.65
                            and not kind
                        ):
                            kind = "lps"
                            event_floor = pull["low"]
                            event_origin = times[pull["at"]]
                            contraction = ratio
                            leg = None
            if kind:
                support, formed, known = event_floor, event_origin, when
        warning_active = warning is not None
        score = 0.0
        if kind:
            score = float(
                np.clip(
                    70
                    + 10 * np.clip((loc - 0.5) / 0.5, 0, 1)
                    + 10 * np.clip(1 - contraction / cfg.contraction_ratio, 0, 1)
                    + 10 * min(v / 2, 1),
                    0,
                    100,
                )
            )
        failed = bool(math.isfinite(support) and ready and c < support - 0.15 * atr)
        rows.append(
            dict(
                event=kind,
                score=score,
                support=support,
                support_origin=formed,
                support_known=known,
                event_floor=event_floor,
                event_origin=event_origin,
                contraction=contraction,
                supply_warning=warning_active,
                supply_defense=bool(warning and warning["confirmed"]),
                structure_failed=failed,
                sign_of_strength=bool(sos),
                attack=bool(
                    not warning_active
                    and (kind or (sos and math.isfinite(support) and c > support))
                ),
                demand_ready=bool(
                    ready
                    and math.isfinite(support)
                    and c > support
                    and not warning_active
                ),
            )
        )
    for key in rows[0]:
        f[key] = [r[key] for r in rows]
    return f
