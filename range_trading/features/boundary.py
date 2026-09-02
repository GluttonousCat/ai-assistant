"""
价格边界类特征: Quantile 边界 / RangePos / 边界稳定性 / 价格密度 / 触碰与反应

【边界约束】(指南第九节) 不使用 rolling max/min 作为边界 (异常 spike 会污染区间),
统一使用分位数边界 Upper=Q(0.9) / Lower=Q(0.1)。
所有指标只用 t 及之前的数据; 边界反应 (MFE) 的未来窗口通过滞后回填消除前视。
"""
from __future__ import annotations

import pandas as pd

from range_trading.features.directional import EPS


def quantile_boundary(close: pd.Series, window: int,
                      upper_q: float, lower_q: float) -> tuple[pd.Series, pd.Series]:
    """滚动分位数边界 (基于收盘价分布的 Price Acceptance Area)"""
    upper = close.rolling(window, min_periods=window).quantile(upper_q)
    lower = close.rolling(window, min_periods=window).quantile(lower_q)
    return upper, lower


def range_mid(upper: pd.Series, lower: pd.Series) -> pd.Series:
    return (upper + lower) / 2.0


def range_pos(close: pd.Series, upper: pd.Series, lower: pd.Series) -> pd.Series:
    """
    RangePos = (Price - Lower) / (Upper - Lower) (指南第十节)。
    不做裁剪: 价格越界时 <0 或 >1 本身携带信息 (假突破/突破)。
    """
    return (close - lower) / (upper - lower + EPS)


def range_width(upper: pd.Series, lower: pd.Series) -> pd.Series:
    """RangeWidth = (Upper - Lower) / Mid, 相对宽度"""
    mid = range_mid(upper, lower)
    return (upper - lower) / (mid + EPS)


def _norm_slope(series: pd.Series, k: int, ref: pd.Series) -> pd.Series:
    """归一化斜率: (x_t - x_{t-k}) / k / ref, 单位为 '每日变化占 ref 的比例'"""
    return (series - series.shift(k)) / (k + EPS) / (ref + EPS)


def boundary_stability(upper: pd.Series, lower: pd.Series,
                       k: int, slope_ref: float) -> tuple[pd.Series, pd.Series]:
    """
    边界稳定性 (指南第十一节):
        BoundaryStability = 1 - |NormalizedSlope| / slope_ref, 裁剪到 [0,1]
    上/下边界各自输出; 边界日均移动达到 slope_ref (占 mid 比例) 即完全不稳定。
    """
    mid = range_mid(upper, lower)
    up_stab = (1.0 - _norm_slope(upper, k, mid).abs() / slope_ref).clip(0.0, 1.0)
    lo_stab = (1.0 - _norm_slope(lower, k, mid).abs() / slope_ref).clip(0.0, 1.0)
    return up_stab, lo_stab


def width_stability(width: pd.Series, k: int, slope_ref: float) -> pd.Series:
    """WidthStability = 1 - |ΔWidth| / k / slope_ref, 裁剪到 [0,1]"""
    return (1.0 - (width - width.shift(k)).abs() / (k + EPS) / slope_ref).clip(0.0, 1.0)


def price_density(close: pd.Series, upper: pd.Series, lower: pd.Series,
                  window: int) -> pd.Series:
    """
    价格密度 / Price Acceptance (指南 15.2C):
    窗口内收盘价落在当前 [Lower, Upper] 区间内的比例, 衡量价格对该区域的接受程度。
    """
    inside = ((close >= lower) & (close <= upper)).astype(float)
    return inside.rolling(window, min_periods=window // 2).mean()


def touch_count(high: pd.Series, low: pd.Series, upper: pd.Series, lower: pd.Series,
                window: int, band: float) -> pd.Series:
    """
    边界触碰次数 (指南第十二节): 窗口内 high/low 进入边界带 (区间宽度的 band 比例) 的次数,
    上/下触碰合并计数。触碰是边界反应研究的前置事件。
    """
    bw = (upper - lower) * band
    up_touch = (high >= upper - bw)
    lo_touch = (low <= lower + bw)
    return (up_touch.astype(float) + lo_touch.astype(float)).rolling(
        window, min_periods=window // 2
    ).sum()


def boundary_rejection(high: pd.Series, low: pd.Series, close: pd.Series,
                       atr_series: pd.Series, upper: pd.Series, lower: pd.Series,
                       band: float, horizon: int, window: int,
                       volume_ratio: pd.Series | None = None) -> tuple[pd.Series, pd.Series]:
    """
    边界反应强度 (指南第十二节):
        SupportResponse   = Mean(MFE_after_touch / ATR)   下沿触碰后最大上行
        ResistanceResponse = Mean(MAE_after_touch / ATR)  上沿触碰后最大下行

    无前视处理: 触碰发生在 j, 其 horizon 天内的最大有利偏移在 j+horizon 才完全可知,
    因此把反应值 shift(horizon) 回填到成熟时刻, 再在过去 window 个交易日上取均值。

    量能加权: 传入 volume_ratio 时按触碰日量比加权 (放量触碰的反转证据更可信),
    权重与反应同样滞后 horizon 回填, 口径与等权版一致。
    """
    bw = (upper - lower) * band
    lo_touch = low <= lower + bw
    up_touch = high >= upper - bw

    # forward_max[t] = max(high[t+1..t+h]) / min(low[t+1..t+h]) (仅内部使用)
    fwd_high = high.rolling(horizon).max().shift(-horizon)
    fwd_low = low.rolling(horizon).min().shift(-horizon)

    sup_reaction = ((fwd_high - close) / (atr_series + EPS)).where(lo_touch)
    res_reaction = ((close - fwd_low) / (atr_series + EPS)).where(up_touch)

    def _mean(r: pd.Series) -> pd.Series:
        return r.shift(horizon).rolling(window, min_periods=1).mean()

    def _weighted_mean(r: pd.Series, touch: pd.Series) -> pd.Series:
        w = volume_ratio.where(touch)
        r_known = r.shift(horizon)
        w_known = w.shift(horizon)
        num = (r_known * w_known).rolling(window, min_periods=1).sum()
        den = w_known.rolling(window, min_periods=1).sum()
        return num / (den + EPS)

    if volume_ratio is None:
        support_response = _mean(sup_reaction)
        resistance_response = _mean(res_reaction)
    else:
        support_response = _weighted_mean(sup_reaction, lo_touch)
        resistance_response = _weighted_mean(res_reaction, up_touch)
    return support_response, resistance_response
