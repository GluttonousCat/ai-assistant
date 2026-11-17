"""Prefix-only indicators and time-keyed rule evaluation."""

import numpy as np
import pandas as pd

from skills.quant.rule_models import Constant, Group, Indicator

PRICE_METRICS = {
    "open",
    "high",
    "low",
    "close",
    "raw_close",
    "ma",
    "ema",
    "atr",
    "prior_high",
    "prior_low",
    "macd_dif",
    "macd_dea",
    "macd_hist",
}


def indicator(frame, ref):
    close, n = frame.close, ref.period
    metric = ref.metric
    if metric in ("open", "high", "low", "close", "raw_close", "volume"):
        values = frame[metric]
    elif metric == "ma":
        values = close.rolling(n, min_periods=n).mean()
    elif metric == "ema":
        values = close.ewm(span=n, adjust=False, min_periods=n).mean()
    elif metric == "rsi":
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        loss = (
            (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        )
        values = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        values = values.mask(loss.eq(0) & gain.gt(0), 100).mask(
            loss.eq(0) & gain.eq(0), 50
        )
    elif metric == "atr":
        prior = close.shift()
        true_range = pd.concat(
            [
                frame.high - frame.low,
                (frame.high - prior).abs(),
                (frame.low - prior).abs(),
            ],
            axis=1,
        ).max(axis=1)
        values = true_range.rolling(n, min_periods=n).mean()
    elif metric.startswith("macd_"):
        dif = (
            close.ewm(span=12, adjust=False, min_periods=26).mean()
            - close.ewm(span=26, adjust=False, min_periods=26).mean()
        )
        dea = dif.ewm(span=9, adjust=False, min_periods=9).mean()
        values = {"macd_dif": dif, "macd_dea": dea, "macd_hist": dif - dea}[metric]
    elif metric == "volume_ratio":
        values = frame.volume / frame.volume.shift().rolling(
            n, min_periods=n
        ).mean().replace(0, np.nan)
    elif metric == "prior_high":
        values = frame.high.shift().rolling(n, min_periods=n).max()
    elif metric == "prior_low":
        values = frame.low.shift().rolling(n, min_periods=n).min()
    elif metric == "return_pct":
        values = (close / close.shift(n) - 1) * 100
    else:
        raise ValueError("未知指标")
    return pd.Series(
        values.shift(ref.lag).to_numpy(copy=True) * ref.multiplier,
        index=pd.DatetimeIndex(frame.bar_end),
    )


class Evaluator:
    def __init__(self, minute, daily, clock):
        self.frames = {"60F": minute, "1D": daily}
        self.clock = pd.DatetimeIndex(clock)
        self.cache = {}

    def operand(self, ref):
        if isinstance(ref, Constant):
            return pd.Series(ref.value, index=self.clock)
        key = ref.model_dump_json()
        if key not in self.cache:
            frame = self.frames[ref.timeframe]
            if frame.empty:
                values = pd.Series(np.nan, index=self.clock)
            else:
                values = indicator(frame, ref)
                # Only completed daily bars are visible at an intraday timestamp.
                values = (
                    values.reindex(self.clock, method="ffill")
                    if ref.timeframe == "1D"
                    else values.reindex(self.clock)
                )
            self.cache[key] = values
        return self.cache[key]

    def evaluate(self, node):
        if node is None:
            return pd.Series(False, index=self.clock)
        if isinstance(node, Group):
            results = pd.concat(
                [self.evaluate(child) for child in node.children], axis=1
            )
            return results.all(axis=1) if node.logic == "all" else results.any(axis=1)
        left, right = self.operand(node.left), self.operand(node.right)
        tolerance = self.tolerance(node, left, right)
        valid = np.isfinite(left) & np.isfinite(right)
        if node.operator.startswith("cross_"):
            # Evaluate a daily-only crossover on its native clock before expansion.
            daily_only = node.left.timeframe == "1D" and (
                isinstance(node.right, Constant) or node.right.timeframe == "1D"
            )
            if daily_only:
                daily_clock = pd.DatetimeIndex(self.frames["1D"].bar_end)
                native = Evaluator(self.frames["60F"], self.frames["1D"], daily_clock)
                a, b = native.operand(node.left), native.operand(node.right)
                crossed = self._cross(a, b, node.operator, native.tolerance(node, a, b))
                # An event is visible at its completion only, never repeated all next day.
                return crossed.reindex(self.clock, fill_value=False).astype(bool)
            return self._cross(left, right, node.operator, tolerance)
        delta = left - right
        comparisons = {
            "gt": delta > tolerance,
            "ge": delta >= -tolerance,
            "lt": delta < -tolerance,
            "le": delta <= tolerance,
        }
        return valid & comparisons[node.operator]

    def tolerance(self, node, left, right):
        floor = np.maximum(np.maximum(left.abs(), right.abs()), 1) * (
            64 * np.finfo(float).eps
        )
        if node.left.metric not in PRICE_METRICS:
            return floor
        atr = self.operand(
            Indicator(
                metric="atr",
                period=14,
                timeframe=node.left.timeframe,
                lag=node.left.lag,
            )
        ).abs()
        if node.left.metric == "raw_close":
            adjusted = self.operand(
                Indicator(
                    metric="close", timeframe=node.left.timeframe, lag=node.left.lag
                )
            )
            atr *= (left / node.left.multiplier / adjusted).abs()
        return pd.Series(
            np.fmax(floor, atr * 1e-8 * node.left.multiplier), index=self.clock
        )

    @staticmethod
    def _cross(left, right, operator, tolerance):
        valid = (
            np.isfinite(left)
            & np.isfinite(right)
            & np.isfinite(left.shift())
            & np.isfinite(right.shift())
        )
        return valid & (
            (
                (left - right).gt(tolerance)
                & (left - right).shift().le(tolerance.shift())
            )
            if operator == "cross_up"
            else (
                (left - right).lt(-tolerance)
                & (left - right).shift().ge(-tolerance.shift())
            )
        )
