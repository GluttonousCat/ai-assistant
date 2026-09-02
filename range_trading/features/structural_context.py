# -*- encoding: utf-8 -*-
"""
大结构判定 (Structural Context)

【核心逻辑转换】从"找区间"变成"先找强势结构, 再在强势结构里等整理买点"。

区间本身没有价值, 价值在于区间所处的"大结构":
  - 兆易/胜宏式: 强势上涨后的高位窄幅整理 (上涨中继, 蓄势)
  - 生益沪电式: 震荡上行 (HH/HL 抬升的台阶式上涨)
  - 医药生物式: 下跌趋势中的弱势横盘 (假区间, 下跌中继)  <- 要坚决排除

判定输出 5 类结构状态, 全部用 t 及之前数据 (无前视):
  STRONG_UPTREND    强势上升 (回调/整理的优质土壤)
  UP_CHANNEL        震荡上行 (HH/HL 抬升, 净位移为正)
  BOTTOM_REVERSAL   底部企稳 (长期下跌后放量不破低)
  WEAK_CONSOLIDATION 弱势横盘 (下跌中的躺平, 伪区间)  <- Gate 排除
  DOWNTREND         明确下跌
  NEUTRAL           其他/数据不足

优质买点只允许出现在前三类结构 (STRUCTURAL_OK)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from range_trading.features.directional import EPS

# 结构状态常量
STRONG_UPTREND = "STRONG_UPTREND"        # 强势上升
UP_CHANNEL = "UP_CHANNEL"                # 震荡上行
BOTTOM_REVERSAL = "BOTTOM_REVERSAL"      # 底部企稳反转
WEAK_CONSOLIDATION = "WEAK_CONSOLIDATION"  # 弱势横盘 (伪区间)
DOWNTREND = "DOWNTREND"                  # 下跌趋势
NEUTRAL = "NEUTRAL"                      # 其他

# 优质结构集合 (允许整理买点的土壤)
STRUCTURAL_OK = {STRONG_UPTREND, UP_CHANNEL, BOTTOM_REVERSAL}

STATE_LABEL_CN = {
    STRONG_UPTREND: "强势上升",
    UP_CHANNEL: "震荡上行",
    BOTTOM_REVERSAL: "底部企稳",
    WEAK_CONSOLIDATION: "弱势横盘",
    DOWNTREND: "下跌趋势",
    NEUTRAL: "中性",
}


def _rolling_slope(series: pd.Series, window: int) -> pd.Series:
    """归一化斜率: (x_t - x_{t-window}) / window / x_t, 跨价格量纲可比"""
    return (series - series.shift(window)) / (window + EPS) / (series + EPS)


def structural_context(close: pd.Series, high: pd.Series, low: pd.Series,
                       vol: pd.Series, ma_long: int = 120,
                       hh_window: int = 20, ret_long: int = 120) -> pd.Series:
    """
    判定每个交易日的结构状态 (输出字符串列, 与 close 等长对齐)。

    判定优先级 (自上而下, 先强后弱):
      1. STRONG_UPTREND: 价格在长均线上方 且 长均线向上 且 长期收益显著为正
      2. UP_CHANNEL:     不满足1, 但 HH/HL 抬升 且 净位移为正 (震荡上行)
      3. BOTTOM_REVERSAL: 长期下跌后, 放量但价格守住前低 (底部承接)
      4. DOWNTREND:      价格在长均线下方 且 长均线向下
      5. WEAK_CONSOLIDATION: 下跌趋势中的窄幅横盘 (伪区间)
      6. NEUTRAL:        其余
    """
    n = len(close)
    out = pd.Series(NEUTRAL, index=close.index, dtype=object)

    ma = close.rolling(ma_long, min_periods=ma_long).mean()
    above_ma = close > ma
    ma_slope = _rolling_slope(ma, 20)                  # 长均线 20 日斜率
    ret_l = close.pct_change(ret_long)                 # 长期累计收益
    net = _rolling_slope(close, 60)                    # 60 日净位移斜率

    hh = (high > high.shift(1)).astype(float).rolling(hh_window, min_periods=hh_window).mean()
    hl = (low > low.shift(1)).astype(float).rolling(hh_window, min_periods=hh_window).mean()

    # 量能趋势: 20 日均量相对 60 日均量
    vol_trend = vol.rolling(20).mean() / (vol.rolling(60).mean() + EPS)
    # 前低 (底部判定的参照): 过去 60 日最低
    prev_low = low.rolling(60, min_periods=60).min().shift(1)
    hold_low = low >= prev_low * 0.99                  # 不破前低 (1% 容差)
    # 长期下跌: 250 日收益显著为负
    ret_250 = close.pct_change(250)

    valid = ma.notna() & ret_l.notna()

    for i in range(n):
        if not valid.iloc[i]:
            out.iloc[i] = NEUTRAL
            continue
        a = bool(above_ma.iloc[i])
        ma_up = ma_slope.iloc[i] > 0
        r_l = ret_l.iloc[i]
        r250 = ret_250.iloc[i] if not np.isnan(ret_250.iloc[i]) else 0.0
        net_v = net.iloc[i]
        hh_v = hh.iloc[i]
        hl_v = hl.iloc[i]
        vt = vol_trend.iloc[i] if not np.isnan(vol_trend.iloc[i]) else 1.0
        hold = bool(hold_low.iloc[i]) if not np.isnan(prev_low.iloc[i]) else False

        # 1. 强势上升: 线上 + 均线向上 + 长期收益 > 15%
        if a and ma_up and r_l > 0.15:
            out.iloc[i] = STRONG_UPTREND
        # 2. 震荡上行: HH/HL 抬升 (双率 > 0.5) 且 净位移为正
        elif (not np.isnan(hh_v) and not np.isnan(hl_v)
              and hh_v > 0.5 and hl_v > 0.5 and net_v > 0):
            out.iloc[i] = UP_CHANNEL
        # 3. 底部企稳: 长期深跌 (250日 < -25%) + 放量 + 守住前低
        elif r250 < -0.25 and vt > 1.2 and hold:
            out.iloc[i] = BOTTOM_REVERSAL
        # 4. 明确下跌: 线下 + 均线向下
        elif (not a) and (not ma_up):
            # 进一步区分: 窄幅横盘(伪区间) vs 流畅下跌
            # 弱势横盘: 净位移小 (|net| < 0.05%) 且 量能萎缩
            if abs(net_v) < 0.0005 and vt < 1.0:
                out.iloc[i] = WEAK_CONSOLIDATION
            else:
                out.iloc[i] = DOWNTREND
        else:
            out.iloc[i] = NEUTRAL
    return out


def is_structural_ok(state: pd.Series) -> pd.Series:
    """布尔列: 是否处于允许整理买点的优质结构"""
    return state.isin(STRUCTURAL_OK)
