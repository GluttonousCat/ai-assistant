"""
方向效率类特征: Directional Inefficiency / DI 斜率与加速度 / FlipRate / AC1

【边界约束】本模块只做纯计算 (输入 Series -> 输出 Series, 对齐 index),
不读写数据库, 不包含任何阈值判断 (指南第五十一章、五十二章)。
所有函数只使用 t 时刻及之前的数据, 无未来函数。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12


def efficiency_ratio(close: pd.Series, window: int) -> pd.Series:
    """
    Efficiency Ratio (Kaufman):
        ER_N = |C_t - C_{t-N}| / (sum_{i=1..N} |C_i - C_{i-1}| + eps)
    趋势 -> ER 接近 1; 震荡 -> ER 接近 0。
    """
    path = close.diff().abs().rolling(window, min_periods=window).sum()
    displacement = close.diff(window).abs()
    return displacement / (path + EPS)


def directional_inefficiency(close: pd.Series, window: int) -> pd.Series:
    """
    Directional Inefficiency: DI = 1 - ER
    判断的不是 "价格有没有波动", 而是 "波动有没有产生方向"。(指南第三节)
    """
    return 1.0 - efficiency_ratio(close, window)


def di_slope(di: pd.Series, k: int) -> pd.Series:
    """DITrend = DI_t - DI_{t-k}; 最重要的不是 DI 水平而是 DI 的变化 (指南第四节)"""
    return di - di.shift(k)


def di_acceleration(di: pd.Series, k: int) -> pd.Series:
    """DIAcceleration = ΔDI_t - ΔDI_{t-1}, 其中 ΔDI_t = DI_t - DI_{t-k}"""
    delta = di - di.shift(k)
    return delta - delta.shift(1)


def flip_rate(close: pd.Series, window: int) -> pd.Series:
    """
    Return Flip Rate (指南第六节):
        FlipRate_N = sum_{i=2..N} I(sign(r_i) != sign(r_{i-1})) / (N-1)
    趋势 (+ + + +) -> 低; 震荡 (+ - + -) -> 高。平盘 r=0 记为独立符号。
    """
    sign = np.sign(close.diff())
    flip = (sign != sign.shift(1)).astype(float)
    flip = flip.where(sign.notna() & sign.shift(1).notna(), np.nan)
    return flip.rolling(window, min_periods=window).sum() / (window - 1)


def autocorr1(close: pd.Series, window: int) -> pd.Series:
    """
    一阶收益自相关 AC1 = Corr(r_t, r_{t-1}) (指南第七节)。
    震荡通常 AC1 < 0; 但不可单独使用, 只作为组合证据。
    """
    r = close.diff()
    return r.rolling(window, min_periods=window).corr(r.shift(1))
