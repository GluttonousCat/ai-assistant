"""
趋势结构类特征: EMA 斜率 / 净位移斜率 / HH-HL 率
+ 趋势系统扩展: 摆动点 / 回调深度 / 结构完整性 / 结构破坏

用于两件事:
1. TrendFailure 打分中的 "斜率趋零" 证据 (指南 15.2B: 关注 Slope->0 而非 EMA20<EMA60);
2. Trend Pullback Filter / 高波动趋势过滤 (指南第三十七~三十九节的假阳性防护);
3. 趋势系统的结构三件套 (趋势指南第四章): 结构完整性 / 回调深度 / 结构破坏。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from range_trading.features.directional import EPS


def ema(close: pd.Series, window: int) -> pd.Series:
    return close.ewm(span=window, adjust=False, min_periods=window).mean()


def ema_slope(close: pd.Series, window: int, k: int) -> pd.Series:
    """
    EMA 归一化斜率: (EMA_t - EMA_{t-k}) / k / Close_t
    单位为 "每日价格变化比例" (如 0.001 = 日均 0.1%), 跨价格量纲可比。
    """
    e = ema(close, window)
    return (e - e.shift(k)) / (k + EPS) / (close + EPS)


def net_slope(close: pd.Series, window: int) -> pd.Series:
    """
    净位移斜率: (C_t - C_{t-window}) / window / C_t
    高波动趋势 (DI 低但路径长) 的识别依赖净位移是否持续为正/负 (指南第三十九节)。
    """
    return (close - close.shift(window)) / (window + EPS) / (close + EPS)


def higher_high_rate(high: pd.Series, window: int) -> pd.Series:
    """窗口内 High > 前日 High 的比例 (趋势延续证据)"""
    hh = (high > high.shift(1)).astype(float)
    return hh.rolling(window, min_periods=window).mean()


def higher_low_rate(low: pd.Series, window: int) -> pd.Series:
    """窗口内 Low > 前日 Low 的比例 (趋势延续证据)"""
    hl = (low > low.shift(1)).astype(float)
    return hl.rolling(window, min_periods=window).mean()


# ==================== 趋势系统扩展 (趋势指南第四章) ====================

def last_swing_extreme(series: pd.Series, window: int, is_high: bool,
                        confirm_lag: int = 5) -> pd.Series:
    """
    最近已确认摆动极值 (Swing High/Low)。

    摆动点定义: 某根K的 high 是前后 window 根中的最高 (swing high, 分形);
    右侧 confirm_lag 根走完后该点才"确认" (无前视)。
    输出: 截至时刻 t, 最近一个已确认摆动点的值 (未确认前为 NaN)。
    """
    if is_high:
        center = series.rolling(2 * window + 1, min_periods=window + 1).max()
        is_extreme = series >= center
    else:
        center = series.rolling(2 * window + 1, min_periods=window + 1).min()
        is_extreme = series <= center
    val = series.where(is_extreme)
    # 滞后 confirm_lag (右侧窗口) 后才可见
    return val.shift(confirm_lag).ffill()


def pullback_depth(high: pd.Series, low: pd.Series, close: pd.Series,
                   window: int, confirm_lag: int = 5) -> pd.Series:
    """
    回调深度 (趋势指南 4.2):
        Depth = (LastSwingHigh - Close) / (LastSwingHigh - LastSwingLow)
    < 0.4 健康回调; 0.4~0.6 深度回调; > 0.6 结构风险。
    """
    sh = last_swing_extreme(high, window, True, confirm_lag)
    sl = last_swing_extreme(low, window, False, confirm_lag)
    depth = (sh - close) / (sh - sl + EPS)
    return depth.where((sh - sl) > 0)          # 波段幅度为 0 时无效


def structure_integrity(high: pd.Series, low: pd.Series, window: int) -> pd.Series:
    """
    结构完整性 (趋势指南 4.1): (HH数 + HL数) / (结构点总数)。
    直接用滚动 HH/HL 率合成 (与现有 hh_rate/hl_rate 同口径):
        Integrity = (hh_rate + hl_rate) / 2, > 0.75 强趋势结构
    """
    hh = (high > high.shift(1)).astype(float)
    hl = (low > low.shift(1)).astype(float)
    hh_rate = hh.rolling(window, min_periods=window).mean()
    hl_rate = hl.rolling(window, min_periods=window).mean()
    return (hh_rate + hl_rate) / 2.0


def hl_break(low: pd.Series, close: pd.Series, window: int,
             confirm_lag: int = 5) -> pd.Series:
    """
    结构破坏 (趋势指南 4.1): 收盘跌破最近已确认的摆动低点。
    True = 第一个结构警告 (Trend Invalidation 三重证据之一)。
    """
    sl = last_swing_extreme(low, window, False, confirm_lag)
    return close < sl
