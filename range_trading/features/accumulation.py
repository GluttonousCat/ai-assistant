# -*- encoding: utf-8 -*-
"""
早期蓄势识别 (Early Accumulation Detector)

识别"正在蓄势 + 缩量消化 + 结构未被破坏"的启动候选:
在标的走出主升浪/强势反弹之前的第一波整理期就标记出来。

【设计来源】紫金矿业案例 (601899.SH):
  2026-06 深跌 (ret120≈-25%) -> 7.1 放量反弹 (>+10%) -> 7.3~7.13 缩量窄幅整理 (7天)
  -> 7.14 放量启动走出主升浪。
  关键特征: 长期深跌 + 反弹启动 + 缩量消化 + 守住低点 + 板块共振。

【满分参考 (经全市场 2026-07-13 抽样校准, n=513)】
  全市场蓄势候选: 未来10日均值 +0.6% 胜率54% (弱正)
  有色金属板块内: 未来10日均值 +10.8% 胜率83% (强正, 大资金抱团)

输出: acc_score (0~100)  +  accum_state (枚举), 供结构判定/看板/回测使用。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from range_trading.features.directional import EPS

# 蓄势状态
ACCUMULATING = "ACCUMULATING"        # 蓄势中 (主升浪/强势反弹候选)
REVERSAL_BOUNCE = "REVERSAL_BOUNCE"  # 反弹启动 (刚放量反弹, 未到缩量确认)
BELOW_LOW = "BELOW_LOW"              # 跌破结构 (蓄势失败)
NO_SIGNAL = "NO_SIGNAL"              # 无蓄势特征


def accumulation_score(close: pd.Series, high: pd.Series, low: pd.Series,
                       vol: pd.Series) -> pd.DataFrame:
    """
    计算逐日蓄势评分。返回 DataFrame 含:
      acc_rebound  反弹强度 (相对30日低点)
      acc_shrink   缩量程度 (近3日均量/近10日均量, <1 缩量)
      acc_narrow   整理窄幅度 (近5日振幅% )
      acc_hold     守住近10日低点
      acc_score    综合评分 0~100 (越高越像蓄势)
      accum_state  状态枚举
    """
    n = len(close)
    close = close.astype(float)
    vol = vol.astype(float)

    # 长期跌幅: 120 日
    ret120 = close.pct_change(120)
    # 反弹: 相对近30日最低点的反弹幅度
    low30 = low.rolling(30, min_periods=30).min()
    rebound = (close - low30) / (low30 + EPS)
    # 缩量: 近3日均量 vs 近10日均量
    vol3 = vol.rolling(3, min_periods=3).mean()
    vol10 = vol.rolling(10, min_periods=10).mean()
    shrink = vol3 / (vol10 + EPS)
    # 窄幅: 近5日振幅
    rng5 = (high.rolling(5, min_periods=5).max() - low.rolling(5, min_periods=5).min()) / (close + EPS)
    # 守低: 收盘不低于近10日低点的 98%
    low10 = low.rolling(10, min_periods=10).min()
    hold = (close >= low10 * 0.98).astype(float)

    out = pd.DataFrame(index=close.index)
    out["acc_rebound"] = rebound
    out["acc_shrink"] = shrink
    out["acc_narrow"] = rng5
    out["acc_hold"] = hold
    out["acc_ret120"] = ret120

    # ---- 评分 (每项 0~1 映射到 0~100) ----
    # 长期深跌: ret120 < -15% 得满分, -15%~0 递减, >0 为0
    deep = (0.0 - ret120) / (0.15 + 1e-9)          # ret120=-15% -> 1.0
    deep = deep.clip(0.0, 1.0).fillna(0.0)
    # 反弹: 3%~12% 线性 (太低=没启动, 太高=已涨完). >15% 视为"反弹已走完"(派发/追高风险), 大幅降分
    reb_ok = ((rebound - 0.03) / 0.09).clip(0.0, 1.0).fillna(0.0)
    reb_ok = reb_ok.where(rebound < 0.15, 0.15).where(rebound >= 0, 0.1)
    # 缩量: 0.5~1.0 (越缩量分越高)
    shrink_ok = ((1.0 - shrink) / 0.5).clip(0.0, 1.0).fillna(0.0)
    # 窄幅: 振幅<8% 加分, >15% 不经济
    narrow_ok = ((0.15 - rng5) / 0.07).clip(0.0, 1.0).fillna(0.0)

    out["acc_score"] = (
        0.30 * deep + 0.25 * reb_ok + 0.20 * shrink_ok + 0.15 * narrow_ok + 0.10 * hold
    ) * 100.0

    # ---- 状态判定 ----
    # 蓄势: 深跌 + 有反弹(但未走完) + 缩量 + 窄幅 + 守低 (核心判别)
    is_acc = (
        (ret120 < -0.10)
        & (rebound.between(0.03, 0.15))      # 反弹3%~15%: 太低=没启动, 太高=已走完(派发)
        & (shrink < 1.0)
        & (rng5 < 0.15)
        & (hold > 0.5)
    )
    # 反弹启动 (刚放量反弹, 缩量确认前): 深跌+反弹但尚未缩量或反弹偏高
    is_bounce = (
        (ret120 < -0.10)
        & (rebound > 0.05)
        & ~is_acc
    )
    out["accum_state"] = np.where(
        is_acc, ACCUMULATING,
        np.where(is_bounce, REVERSAL_BOUNCE,
                 np.where((ret120 < -0.10) & (close < low10 * 0.97), BELOW_LOW, NO_SIGNAL)))

    return out


# 蓄势候选 (供结构 Gate / 看板过滤)
def is_accumulating(state: pd.Series) -> pd.Series:
    return (state == ACCUMULATING) | (state == REVERSAL_BOUNCE)