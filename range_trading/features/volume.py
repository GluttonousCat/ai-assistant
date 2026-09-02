"""
量能类特征: 量比 / 量能分位 / OBV 斜率 / VWAP 重心偏离
+ 趋势系统扩展: UDVOL / 量能持续性 / 量价同步 / 资金流 / VWAP 斜率

【角色定位】
- 震荡系统 (指南第二十九章): Volume 是"辅助"证据;
- 趋势系统 (趋势指南第五章): 量价是"硬门槛" (VPA 共振矩阵), 五层体系逐层递进。
全部做时序归一化 (指南第五十三章), 跨标的可比。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from range_trading.features.directional import EPS


def volume_ratio(vol: pd.Series, window: int) -> pd.Series:
    """量比: 当日成交量 / MA(volume, window)"""
    ma = vol.rolling(window, min_periods=window).mean()
    return vol / (ma + EPS)


def volume_pct(vol: pd.Series, window: int, percentile_window: int) -> pd.Series:
    """量能时序分位 (0~1): 复用滑动窗口分位数实现"""
    from range_trading.features.volatility import rolling_percentile
    return rolling_percentile(volume_ratio(vol, window), percentile_window)


def obv_slope(close: pd.Series, vol: pd.Series, window: int, k: int) -> pd.Series:
    """
    OBV 归一化斜率 ∈ [-1, 1]:
        (OBV_t - OBV_{t-k}) / (sum(vol, k) + eps)
    +1 = 上涨日包揽全部放量, -1 = 下跌日包揽全部放量。
    价格处于区间而 |OBV斜率| 持续偏大 -> 量能单边派发/吸筹, 区间可疑。
    """
    direction = np.sign(close.diff()).fillna(0.0)
    obv = (direction * vol).cumsum()
    return (obv - obv.shift(k)) / (vol.rolling(k, min_periods=k).sum() + EPS)


def obv(close: pd.Series, vol: pd.Series) -> pd.Series:
    """基础 OBV 累计线"""
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * vol).cumsum()


def vwap_dev(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series,
             upper: pd.Series, lower: pd.Series, window: int) -> pd.Series:
    """
    量能重心偏离: (VWAP_window - RangeMid) / (Upper - Lower)
    >0 = 成交量重心偏区间上部 (阻力区承接了大量换手), <0 = 偏下部。
    """
    tp = (high + low + close) / 3.0
    pv = (tp * vol).rolling(window, min_periods=window).sum()
    vv = vol.rolling(window, min_periods=window).sum()
    vwap = pv / (vv + EPS)
    mid = (upper + lower) / 2.0
    return (vwap - mid) / (upper - lower + EPS)


def rolling_vwap(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series,
                 window: int) -> pd.Series:
    """滚动 VWAP (成交量加权均价)"""
    tp = (high + low + close) / 3.0
    pv = (tp * vol).rolling(window, min_periods=window).sum()
    vv = vol.rolling(window, min_periods=window).sum()
    return pv / (vv + EPS)


# ==================== 趋势系统扩展 (趋势指南第五章) ====================

def updown_vol_ratio(close: pd.Series, vol: pd.Series, window: int) -> pd.Series:
    """
    UDVOL (指南 5.2): N 日内上涨日成交量之和 / 下跌日成交量之和。
    > 1.5 买方主导 (健康趋势); < 0.8 派发嫌疑。
    """
    up = vol.where(close.diff() > 0, 0.0).rolling(window, min_periods=window).sum()
    down = vol.where(close.diff() < 0, 0.0).rolling(window, min_periods=window).sum()
    return up / (down + EPS)


def vol_persistence(vol: pd.Series, fast: int, slow: int) -> pd.Series:
    """
    量能持续性 (指南 5.2): VolMA_fast / VolMA_slow。
    持续放量 > 1.3; 区分"趋势型放量"与"单日脉冲"。
    """
    return vol.rolling(fast, min_periods=fast).mean() / (
        vol.rolling(slow, min_periods=slow).mean() + EPS)


def vol_pulse_share(vol: pd.Series, window: int = 5) -> pd.Series:
    """
    单日量能脉冲占比: 近 window 日最大单日量 / 量能和。
    > 0.5 说明量能高度集中于一天 (消息面 one-day wonder 特征)。
    """
    total = vol.rolling(window, min_periods=window).sum()
    max_vol = vol.rolling(window, min_periods=window).max()
    return max_vol / (total + EPS)


def price_volume_corr(close: pd.Series, obv_series: pd.Series, window: int) -> pd.Series:
    """
    量价相关系数 (指南 5.3): 价格与 OBV 的滚动相关。
    健康趋势 > 0.7; < 0.3 量价脱钩 (背离前兆)。
    """
    return close.rolling(window, min_periods=window).corr(obv_series)


def obv_confirm(close: pd.Series, obv_series: pd.Series, window: int) -> pd.Series:
    """
    价格-OBV 新高同步性 (指南 5.3), 输出三值:
        1.0 = 双新高 (价格与 OBV 同创 window 日新高, 趋势健康)
        0.5 = 仅价格新高 (顶背离, 衰竭核心证据)
        0.0 = 都未新高
    """
    price_new_high = close >= close.rolling(window, min_periods=window).max()
    obv_new_high = obv_series >= obv_series.rolling(window, min_periods=window).max()
    return (
        price_new_high.astype(float) * 0.5
        + obv_new_high.astype(float) * 0.5
    )


def bearish_divergence(close: pd.Series, obv_series: pd.Series, window: int) -> pd.Series:
    """
    顶背离 (指南 5.3): 价格创 window 日新高而 OBV 未创新高。
    趋势衰竭三重证据之首。
    """
    price_new_high = close >= close.rolling(window, min_periods=window).max()
    obv_new_high = obv_series >= obv_series.rolling(window, min_periods=window).max()
    return price_new_high & ~obv_new_high


def ad_line(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series) -> pd.Series:
    """A/D Line (指南 5.4): CLV × 成交量 的累计线"""
    clv = ((close - low) - (high - close)) / (high - low + EPS)
    return (clv * vol).cumsum()


def cmf(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series,
        window: int) -> pd.Series:
    """
    Chaikin Money Flow (指南 5.4): CLV×量 的窗口和 / 窗口总量。
    > 0.1 资金持续流入; < 0 与上涨方向背离。
    """
    clv = ((close - low) - (high - close)) / (high - low + EPS)
    return (clv * vol).rolling(window, min_periods=window).sum() / (
        vol.rolling(window, min_periods=window).sum() + EPS)


def vwap_slope(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series,
               window: int, k: int) -> pd.Series:
    """
    VWAP 重心斜率 (指南 5.5): (VWAP_t - VWAP_{t-k}) / k / VWAP_t。
    > 0 重心抬升 (多头); 趋势中价格应位于 VWAP 上方且斜率为正。
    """
    vw = rolling_vwap(high, low, close, vol, window)
    return (vw - vw.shift(k)) / (k + EPS) / (vw + EPS)
