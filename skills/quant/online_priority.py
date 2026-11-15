"""Causal monthly 60F priorities learned from completed auxiliary outcomes.

This experimental ranker does not place orders or claim forecast accuracy. All
portfolio fills/risk limits remain in tranche_engine. Auxiliary outcomes are
single-entry price observations, not the portfolio's staged trade returns.
"""

from dataclasses import dataclass, replace
import math

import numpy as np
import pandas as pd

from skills.quant.prepare import swing_frame

VERSION = "causal-market-relative-priority-v21"
FEATURES = (
    "original_score",
    "risk_pct",
    "risk_atr",
    "m21_distance_atr",
    "m13_extension_atr",
    "m21_slope_atr",
    "relative20_atr",
    "relative4_atr",
    "beta",
    "rebound_family",
    "close_held",
    "scale_ready",
    "index_fast",
    "index_trend",
    "index_both_trend",
    "index_avoid_down",
    "index_daily_trend",
    "atr_pct",
    "worst_native60f_downside20_pct",
)


@dataclass(frozen=True)
class OnlineConfig:
    penalty: float = 100
    min_samples: int = 500
    label_horizon: int = 10
    max_exit_delay: int = 10
    label_max_risk_pct: float = 4
    decision_max_risk_pct: float = 3
    label_profit_pct: float = 5
    forecast_floor_pct: float = 0
    label_buy_fee: float = 0.00031
    label_sell_fee: float = 0.00081
    label_slippage: float = 0.0005

    def __post_init__(self):
        for k, v in vars(self).items():
            if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                raise ValueError(f"学习参数必须为有限非负数: {k}")
        if self.penalty <= 0 or not 0 < self.decision_max_risk_pct <= 3:
            raise ValueError("正则化须为正，候选结构风险不得超过3%")
        if not 0 < self.label_max_risk_pct <= 4 or self.label_profit_pct <= 0:
            raise ValueError("辅助标签阈值无效")
        if (
            self.label_slippage >= 0.1
            or self.label_buy_fee >= 1
            or self.label_sell_fee >= 1
        ):
            raise ValueError("辅助费用/滑点不合法")
        for k, minimum in (
            ("min_samples", 2),
            ("label_horizon", 1),
            ("max_exit_delay", 1),
        ):
            if type(getattr(self, k)) is not int or getattr(self, k) < minimum:
                raise ValueError(f"{k}须为合法整数")


def augment_features(minute, stock):
    """Add only completed native-60F volatility, preserving raw execution data."""
    frame, issue = swing_frame(minute, stock)
    if issue:
        raise ValueError(issue)
    price = frame.set_index("bar_end").close
    downside = (-price.pct_change(fill_method=None).rolling(20).min()).clip(
        lower=0
    ) * 100
    data = dict(stock.data)
    data["risk_atr_pct"] = data["atr"] / data["adjusted_close"] * 100
    data["risk_downside20"] = downside.reindex(stock.times).to_numpy(
        dtype=float, copy=True
    )
    return replace(stock, data=data)


def feature_vector(stock, signal):
    d, i = stock.data, signal["i"]
    close, atr = d["adjusted_close"][i], d["atr"][i]
    if not np.isfinite(atr) or atr <= 0:
        return np.full(len(FEATURES), np.nan)
    risk = close - signal["stop"]
    return np.asarray(
        [
            signal["score"],
            risk / close * 100,
            risk / atr,
            (close - d["trail_a"][i]) / atr,
            d["priority_extension"][i],
            d["priority_slope"][i],
            d["priority_relative"][i],
            d["market_excess_pct"][i] / 100 / (atr / close),
            d["market_beta"][i],
            signal["family"] == "base_rebound",
            signal.get("close_held", False),
            d["scale_in_ready"][i],
            *[
                d["market_mode_" + m][i]
                for m in (
                    "any_fast",
                    "any_trend",
                    "both_trend",
                    "avoid_both_down",
                    "daily_trend",
                )
            ],
            d["risk_atr_pct"][i],
            d["risk_downside20"][i],
        ],
        dtype=float,
    )


def auxiliary_outcome(stock, signal, clock_at, cfg=OnlineConfig()):
    """Historical close trigger, next legal open; unresolved labels stay unknown.

    T+1 is respected. Index-relative adjustment is applied by training_rows only
    after this exit is observable. No availability of a future label may decide
    whether a present-day candidate receives a forecast.
    """
    d, i = stock.data, signal["i"]
    first = i + 1
    unknown = (np.nan, pd.Timestamp.max)
    slip = cfg.label_slippage
    if first >= len(stock.times) or not np.isfinite(d["up_limit"][first]):
        return unknown
    if d["open"][first] * (1 + slip) >= d["up_limit"][first]:
        return unknown
    if (
        clock_at[stock.times[first]] != clock_at[stock.times[i]] + 1
        or d["volume"][first] <= 0
    ):
        return unknown
    first_clock = clock_at[stock.times[first]]
    entry = d["adjusted_open"][first] * (1 + slip)
    if not 0 < (entry - signal["stop"]) / entry <= cfg.label_max_risk_pct / 100:
        return unknown
    target = entry * (1 + cfg.label_profit_pct / 100)
    trigger = None
    for j in range(first, len(stock.times)):
        close = d["adjusted_close"][j]
        if (
            close <= signal["stop"]
            or close >= target
            or clock_at[stock.times[j]] >= first_clock + cfg.label_horizon - 1
        ):
            trigger = j
            break
    if trigger is None:
        return unknown
    for j in range(
        trigger + 1, min(trigger + cfg.max_exit_delay + 1, len(stock.times))
    ):
        if (
            clock_at[stock.times[j]]
            > clock_at[stock.times[trigger]] + cfg.max_exit_delay
        ):
            break
        limit = d["down_limit"][j]
        if (
            np.isfinite(limit)
            and d["volume"][j] > 0
            and d["open"][j] * (1 - slip) > limit
        ):
            out = d["adjusted_open"][j] * (1 - slip)
            net = (
                out * (1 - cfg.label_sell_fee) / (entry * (1 + cfg.label_buy_fee)) - 1
            ) * 100
            return float(net), stock.times[j]
    return unknown


def index_opens(frames):
    if set(frames) != {"000300.SH", "000852.SH"}:
        raise ValueError("学习需要两组独立指数原生60F")
    result = {}
    for code in ("000300.SH", "000852.SH"):
        frame = frames[code]
        times = pd.to_datetime(frame.bar_end)
        first = frame[times.dt.hour.eq(10) & times.dt.minute.eq(30)]
        days = pd.DatetimeIndex(first.bar_end).normalize()
        if (
            days.has_duplicates
            or not np.isfinite(first.open).all()
            or (first.open <= 0).any()
        ):
            raise ValueError("指数开盘日期或价格无效")
        result[code] = pd.Series(first.open.to_numpy(copy=True), index=days)
    return result


def training_rows(stocks, calendar, frames, cfg=OnlineConfig()):
    """Create all candidate rows independently from future label eligibility."""
    clock_at = {t: i for i, t in enumerate(pd.DatetimeIndex(calendar))}
    opens = index_opens(frames)
    records = []
    for stock in stocks:
        for signal in stock.signals:
            x = feature_vector(stock, signal)
            y, known = auxiliary_outcome(stock, signal, clock_at, cfg)
            if np.isfinite(y):
                entry = stock.times[signal["i"] + 1].normalize()
                exit_day = known.normalize()
                moves = [
                    opens[c].get(exit_day, np.nan) / opens[c].get(entry, np.nan) - 1
                    for c in opens
                ]
                y -= x[8] * np.mean(moves) * 100
            records.append(
                (stock.code, signal["i"], stock.times[signal["i"]], x, y, known)
            )
    return records


def fit_monthly(records, cfg=OnlineConfig()):
    """Fit only labels fully known before each month; all scalers use that set."""
    if not records:
        return [], {}
    x = np.asarray([r[3] for r in records], dtype=float)
    y = np.asarray([r[4] for r in records], dtype=float)
    at = pd.DatetimeIndex([r[2] for r in records])
    known = pd.DatetimeIndex([r[5] for r in records])
    finite = np.isfinite(x).all(axis=1)
    models, predictions = [], {}
    for month in sorted(set(at.to_period("M"))):
        train = (
            (at < month.start_time)
            & (known < month.start_time)
            & finite
            & np.isfinite(y)
        )
        if train.sum() < cfg.min_samples:
            models.append(
                dict(month=str(month), trained=False, samples=int(train.sum()))
            )
            continue
        mean, std = x[train].mean(axis=0), x[train].std(axis=0)
        std[std < 1e-8] = 1
        a = np.c_[np.ones(train.sum()), np.clip((x[train] - mean) / std, -5, 5)]
        target = np.clip(y[train], -10, 10)
        rows = np.flatnonzero((at.to_period("M") == month) & finite)
        b = np.c_[np.ones(len(rows)), np.clip((x[rows] - mean) / std, -5, 5)]
        reg = np.eye(a.shape[1]) * cfg.penalty
        reg[0, 0] = 0
        coef = np.linalg.solve(a.T @ a + reg, a.T @ target)
        values = b @ coef
        for n, row in enumerate(rows):
            predictions[(records[row][0], records[row][1])] = float(values[n])
        models.append(
            dict(
                month=str(month),
                trained=True,
                samples=int(train.sum()),
                last_label=str(known[train].max()),
                intercept=float(coef[0]),
                feature_means=mean.tolist(),
                feature_stds=std.tolist(),
                coefficients=dict(zip(FEATURES, coef[1:].tolist())),
            )
        )
    return models, predictions


def apply_predictions(stocks, predictions, cfg=OnlineConfig()):
    """Stay in cash until ready; forecasts use completed 60F evidence only."""
    result = []
    for stock in stocks:
        d = dict(stock.data)
        safe = (d["risk_atr_pct"] <= 99) & (d["risk_downside20"] <= 99)
        signals = []
        for original in stock.signals:
            i = original["i"]
            risk = (
                (d["adjusted_close"][i] - original["stop"])
                / d["adjusted_close"][i]
                * 100
            )
            value = predictions.get((stock.code, i))
            if (
                safe[i]
                and risk <= cfg.decision_max_risk_pct
                and value is not None
                and value >= cfg.forecast_floor_pct
            ):
                signals.append({**original, "score": float(value)})
        d["scale_in_ready"] = d["scale_in_ready"] & safe
        result.append(replace(stock, data=d, signals=signals))
    return result
