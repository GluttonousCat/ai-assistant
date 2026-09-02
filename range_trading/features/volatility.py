"""
波动类特征: ATR / NATR / NATR 时序分位 / ATR 稳定性

高质量震荡 = 低方向 + 足够波动 (指南第八节),
因此波动不是被惩罚的对象, 而是必要条件。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from range_trading.features.directional import EPS


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> pd.Series:
    """Wilder 平滑 ATR (与项目内 tools/kline/indicators.py 同口径)"""
    return true_range(high, low, close).ewm(
        alpha=1.0 / window, adjust=False, min_periods=window
    ).mean()


def natr(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> pd.Series:
    """NATR = ATR_N / Close * 100 (百分比), 跨标的可比"""
    return atr(high, low, close, window) / (close + EPS) * 100.0


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """
    时序分位数: 当前值在过去 window 个值中的百分位 (0~1), NaN-aware。
    用于 NATR / Volume 等跨量纲特征的时序归一化 (指南第五十三章)。

    实现: sliding_window_view 向量化, 避免逐行 apply;
    窗口内有效值不足 window/2 时输出 NaN。
    """
    arr = series.to_numpy(dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    if window >= 2 and n >= 2:
        if n < window:
            # 历史不足 window 时退化为 expanding 分位数, 短历史标的也能输出
            for i in range(n):
                seg = arr[: i + 1]
                seg = seg[~np.isnan(seg)]
                if len(seg) >= 2:
                    out[i] = (seg <= seg[-1]).mean()
        else:
            wins = np.lib.stride_tricks.sliding_window_view(arr, window)
            last = wins[:, -1:]
            valid = ~np.isnan(wins) & ~np.isnan(last)
            le = (wins <= last) & valid
            cnt_valid = valid.sum(axis=1)
            cnt_le = le.sum(axis=1)
            pct = np.where(
                cnt_valid >= max(2, window // 2),
                cnt_le / np.maximum(cnt_valid, 1),
                np.nan,
            )
            out[window - 1:] = pct
    return pd.Series(out, index=series.index)


def atr_stability(atr_series: pd.Series, window: int) -> pd.Series:
    """
    ATR 稳定性: 1 - rolling_mean(|ATR 日变化率|), 裁剪到 [0, 1]。
    波动环境的稳定性是 VolatilityQuality 的辅助证据 (指南第二十章)。
    """
    change = atr_series.pct_change().abs().clip(upper=5.0)
    mean_change = change.rolling(window, min_periods=window // 2).mean()
    return (1.0 - mean_change).clip(lower=0.0, upper=1.0)
