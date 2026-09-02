"""
K线事件 (日K层轻量版)

指南中 Bullish/Bearish Event 的完整定义属于 30F 层 (第二十四、二十五章);
日K层只使用其中两类判定:
- 强实体K (body/expansion, 供后续 Event Study 复用);
- 假突破/假跌破 (False Breakout / False Breakdown, 状态机 BREAKOUT_FAILURE 判定依赖, 第四十节)。
阈值参数由 config 传入, 本模块不写死。
"""
from __future__ import annotations

import pandas as pd

from range_trading.features.directional import EPS
from range_trading.features.volatility import true_range


def body_ratio(open_: pd.Series, high: pd.Series, low: pd.Series,
               close: pd.Series) -> pd.Series:
    """BodyRatio = (Close - Open) / (High - Low), 正为阳线"""
    return (close - open_) / (high - low + EPS)


def expansion_ratio(high: pd.Series, low: pd.Series, close: pd.Series,
                    atr_series: pd.Series) -> pd.Series:
    """ExpansionRatio = TR / ATR"""
    return true_range(high, low, close) / (atr_series + EPS)


def bullish_event(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series,
                  atr_series: pd.Series, body_min: float, exp_min: float) -> pd.Series:
    """
    Bullish Event (指南第二十四节): Close > Open + BodyRatio + 波动扩张 三条件同时成立
    """
    return (
        (close > open_)
        & (body_ratio(open_, high, low, close) > body_min)
        & (expansion_ratio(high, low, close, atr_series) > exp_min)
    )


def bearish_event(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series,
                  atr_series: pd.Series, body_min: float, exp_min: float) -> pd.Series:
    """Bearish Event: 镜像定义"""
    return (
        (close < open_)
        & (body_ratio(open_, high, low, close) < -body_min)
        & (expansion_ratio(high, low, close, atr_series) > exp_min)
    )


def false_breakdown(low: pd.Series, close: pd.Series, lower: pd.Series) -> pd.Series:
    """假跌破: Low < Lower 且 Close 收回 Lower 之上 (指南 Event B, 第四十节)"""
    return (low < lower) & (close > lower)


def false_breakout(high: pd.Series, close: pd.Series, upper: pd.Series) -> pd.Series:
    """假突破: High > Upper 且 Close 收回 Upper 之下 (指南第四十节)"""
    return (high > upper) & (close < upper)
