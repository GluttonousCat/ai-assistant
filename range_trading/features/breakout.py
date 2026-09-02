"""
突破质量特征 (趋势指南第六章: Boundary Escape)

对偶于震荡系统的 Boundary Rejection:
震荡问 "触碰边界后是否被弹回", 趋势问 "越过边界后是否成功逃离"。

突破三要素: 越界(收盘) + 量能(量比) + 站稳(后续不回收)。
假突破过滤 (False Breakout Filter) 是趋势系统的核心防护 (趋势指南 6.3 / 第十八章)。
"""
from __future__ import annotations

import pandas as pd

from range_trading.features.directional import EPS


def breakout_escape(close: pd.Series, upper: pd.Series) -> pd.Series:
    """收盘越界 (不是最高价越界): Close > Upper"""
    return close > upper


def breakout_volume_ok(vol_ratio: pd.Series, threshold: float) -> pd.Series:
    """突破日量能门槛: vol_ratio >= threshold (硬条件)"""
    return vol_ratio >= threshold


def breakout_stability(close: pd.Series, upper: pd.Series, confirm_days: int) -> pd.Series:
    """站稳: 连续 confirm_days 日收盘在 Upper 之上 (滚动窗口判定, 当日可知)"""
    above = (close > upper).astype(float)
    return above.rolling(confirm_days, min_periods=confirm_days).min() > 0


def upper_wick_ratio(open_: pd.Series, high: pd.Series, low: pd.Series,
                     close: pd.Series) -> pd.Series:
    """上影线占比: (High - max(O,C)) / (High - Low); > 0.6 = 日内被拒"""
    body_top = pd.concat([open_, close], axis=1).max(axis=1)
    return (high - body_top) / (high - low + EPS)


def false_breakout_flags(close: pd.Series, vol_ratio: pd.Series,
                         upper: pd.Series, open_: pd.Series, high: pd.Series,
                         low: pd.Series, width: pd.Series, age,
                         vol_min: float, width_max: float = 0.35,
                         age_min: int = 20) -> pd.Series:
    """
    假突破降权条件 (趋势指南 6.3, 任一命中):
    1. 突破日量比 < vol_min (缩量突破);
    2. 源区间宽度过大且年龄长 (宽震荡随机越界): width > width_max 且 age > age_min;
    3. 上影线占比 > 0.6 (日内被拒)。
    """
    thin_volume = vol_ratio < vol_min
    wick_reject = upper_wick_ratio(open_, high, low, close) > 0.6
    wide_range = pd.Series(False, index=close.index)
    if width is not None:
        wide_age = pd.Series(False, index=close.index)
        if age is not None:
            wide_age = age.fillna(0) > age_min
        wide_range = (width > width_max) & wide_age
    broke = breakout_escape(close, upper)
    return broke & (thin_volume | wick_reject | wide_range)


def breakout_retest(close: pd.Series, vol: pd.Series, upper: pd.Series,
                    horizon: int = 5, tol: float = 0.01) -> pd.Series:
    """
    Breakout-Retest 模式 (趋势指南 6.2, 事件驱动视角):
    突破日 j 满足 Escape, 之后 horizon 内存在某日回踩 Upper ±tol 且缩量
    (量 < 突破日量), 则在回踩确认日标记 True。
    实现为"回踩发生当日"的布尔 (当日可知: close 接近 Upper, 前期曾突破)。
    """
    broke = breakout_escape(close, upper)
    recent_break = broke.rolling(horizon, min_periods=1).max() > 0
    near_upper = (close - upper).abs() <= (upper * tol)
    # 回踩缩量: 当日量 < 近 horizon 内突破日最大量
    brk_vol_max = vol.where(broke).rolling(horizon, min_periods=1).max()
    shrinking = vol < brk_vol_max
    return recent_break & near_upper & shrinking & ~broke
