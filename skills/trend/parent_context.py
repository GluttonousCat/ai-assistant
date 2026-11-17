"""Daily parent-wave annotations, independent from frozen entry rules.

A local 60F reversal may finish a pullback inside a surviving daily advance.
Daily averages help establish the earlier advance; its structural anchor is
retained through a longer correction rather than reset by one MA crossover.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from skills.trend.mtf import MtfConfig, features

VERSION = "daily-parent-wave-context-v1"
LABELS = {
    "uptrend": "日K上涨结构",
    "pullback": "日K上涨结构内回调",
    "declining": "日K下跌背景",
    "range": "日K整理背景",
    "unknown": "日K背景不足",
}


@dataclass(frozen=True)
class ParentConfig:
    anchor_window: int = 20
    min_up_bars: int = 3
    max_pullback_bars: int = 40
    min_impulse_pct: float = 5
    min_impulse_atr: float = 3
    max_retrace: float = 0.65
    break_confirm_bars: int = 2

    def __post_init__(self):
        for key in (
            "anchor_window",
            "min_up_bars",
            "max_pullback_bars",
            "break_confirm_bars",
        ):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError("母趋势窗口须为正整数")
        if (
            any(
                not math.isfinite(v) or v <= 0
                for v in (self.min_impulse_pct, self.min_impulse_atr)
            )
            or not 0 < self.max_retrace < 1
        ):
            raise ValueError("母趋势幅度参数无效")


def daily_context(daily, cfg=ParentConfig()):
    if daily.empty:
        return pd.DataFrame(columns=CONTEXT_COLUMNS[:8])
    if pd.to_datetime(daily.bar_end).duplicated().any():
        raise ValueError("母趋势日K时间重复")
    f = features(daily, MtfConfig())
    if (
        not np.isfinite(f[["open", "high", "low", "close"]]).all().all()
        or (f[["open", "high", "low", "close"]] <= 0).any().any()
    ):
        raise ValueError("母趋势价格无效")
    floor = f.low.shift().rolling(cfg.anchor_window).min()
    floor_values = floor.to_numpy()
    slow_values = f.ma21.to_numpy()
    closes = f.close.to_numpy()
    ready_values = (
        np.isfinite(f[["ma13", "ma21", "atr"]]).all(axis=1).to_numpy()
        & np.isfinite(floor_values)
        & f.atr.gt(0).to_numpy()
    )
    leg, consecutive, broken = None, 0, 0
    output = []
    for i, r in enumerate(f.itertuples()):
        eps = 1e-8 * r.atr
        ready = i >= 25 and ready_values[i]
        up = (
            ready
            and r.ma13 > r.ma21 + eps
            and r.ma21 > slow_values[i - 5] + eps
            and r.close >= r.ma13 - eps
        )
        consecutive = consecutive + 1 if up else 0
        if leg:
            impulse = leg["peak"] - leg["floor"]
            invalid = (
                r.close < leg["floor"] - 0.25 * r.atr
                or leg["peak"] - r.close > cfg.max_retrace * impulse
            )
            broken = broken + 1 if invalid else 0
            if broken >= cfg.break_confirm_bars or (
                leg["pull"] is not None and i - leg["pull"] >= cfg.max_pullback_bars
            ):
                leg = None
        if leg is None and consecutive >= cfg.min_up_bars:
            impulse = r.high - floor_values[i]
            if (
                impulse >= cfg.min_impulse_atr * r.atr
                and impulse / floor_values[i] * 100 >= cfg.min_impulse_pct
            ):
                leg = dict(
                    origin=r.bar_end,
                    floor=float(floor_values[i]),
                    peak=float(r.high),
                    peak_at=i,
                    pull=None,
                )
                broken = 0
        if leg:
            if r.close > leg["peak"] + eps:
                leg.update(peak=float(r.high), peak_at=i, pull=None)
            elif (
                leg["pull"] is None
                and i > leg["peak_at"]
                and r.close < closes[i - 1] - eps
                and leg["peak"] - r.low >= 0.5 * r.atr
            ):
                leg["pull"] = i
            state = "pullback" if leg["pull"] is not None else "uptrend"
        elif not ready:
            state = "unknown"
        elif (
            r.ma13 < r.ma21 - eps
            and r.ma21 < slow_values[i - 5] - eps
            and r.close < r.ma21
        ):
            state = "declining"
        else:
            state = "range"
        output.append(
            dict(
                parent_context_end=r.bar_end,
                parent_trend=state,
                parent_trend_label=LABELS[state],
                parent_pullback_bars=i - leg["pull"] + 1
                if leg and leg["pull"] is not None
                else 0,
                parent_origin=leg["origin"] if leg else pd.NaT,
                parent_anchor=leg["floor"] if leg else np.nan,
                parent_ma13=r.ma13,
                parent_ma21=r.ma21,
            )
        )
    return pd.DataFrame(output)


def local_kind(row):
    ignition = row.get("reversal_ignition")
    if (
        isinstance(ignition, (bool, np.bool_))
        and ignition
        or row.get("pattern") == "base_rebound"
    ):
        return "reversal"
    if (
        row.get("pattern") == "trend_pullback"
        or row.get("setup_phase") == "pullback_resume"
    ):
        return "resume"
    trend = row.get("trend60")
    if isinstance(trend, dict) and trend.get("fresh"):
        return "reversal"
    return "unknown"


def classification(row, state):
    trigger = local_kind(row)
    if trigger == "unknown":
        return "", ""
    label = "局部止跌反转" if trigger == "reversal" else "局部回调再起"
    if state in ("uptrend", "pullback"):
        return "trend_pullback", label
    if trigger == "reversal" and state in ("declining", "range"):
        return "base_rebound", label
    return "", label


def annotate_chart(data, daily):
    """Only completed daily closes can annotate a given intraday timestamp."""
    if data.empty:
        return data.copy()
    data = data.drop(columns=[c for c in CONTEXT_COLUMNS if c in data]).copy()
    if daily.empty:
        result = data.copy()
        result["parent_trend"] = "unknown"
        result["parent_trend_label"] = LABELS["unknown"]
        return result
    context = daily_context(daily)
    result = pd.merge_asof(
        data.sort_values("bar_end"),
        context.sort_values("parent_context_end"),
        left_on="bar_end",
        right_on="parent_context_end",
        direction="backward",
    )
    result["parent_trend"] = result.parent_trend.fillna("unknown")
    result["parent_trend_label"] = result.parent_trend.map(LABELS)
    kinds = [
        classification(row, row["parent_trend"]) for row in result.to_dict("records")
    ]
    result["context_pattern"] = [k[0] for k in kinds]
    result["local_trigger_label"] = [k[1] for k in kinds]
    return result


def annotate_summary(row, daily, as_of):
    """Add context while preserving every original snapshot signal field."""
    result = {
        k: v
        for k, v in row.items()
        if k not in CONTEXT_COLUMNS
        and k not in ("parent_context_version", "parent_context_issue")
    }
    native = (
        daily[pd.to_datetime(daily.bar_end) <= pd.Timestamp(as_of)]
        if not daily.empty
        else daily
    )
    if native.empty:
        result.update(parent_trend="unknown", parent_trend_label=LABELS["unknown"])
    else:
        context = daily_context(native).iloc[-1]
        for key, value in context.items():
            if isinstance(value, np.generic):
                value = value.item()
            result[key] = (
                value.isoformat()
                if isinstance(value, pd.Timestamp) and not pd.isna(value)
                else value
                if not pd.isna(value)
                else None
            )
        pattern, trigger = classification(row, result["parent_trend"])
        result.update(context_pattern=pattern, local_trigger_label=trigger)
    result["parent_context_version"] = VERSION
    return result


CONTEXT_COLUMNS = (
    "parent_context_end",
    "parent_trend",
    "parent_trend_label",
    "parent_pullback_bars",
    "parent_origin",
    "parent_anchor",
    "parent_ma13",
    "parent_ma21",
    "context_pattern",
    "local_trigger_label",
)
