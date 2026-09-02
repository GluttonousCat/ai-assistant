# -*- encoding: utf-8 -*-
"""
动量矛 (Momentum Spear) —— 科技/成长主升浪识别器

【定位】与"蓝筹蓄势盾" (base_setup) 互补的第二只识别器。
盾抓"缩量阴跌整理后温和启动" (蓝筹波段); 矛抓"大涨后横盘不跌、量能不缩再上攻" (科技主升浪)。

【科技主升浪的真实形态】(用户校准):
  大涨 1~3 天 -> 横盘几天(只是不下跌) -> 再上攻。
  关键区别: 整理期量能不萎缩 (tech 牛有承接), 与蓝筹的缩量整理是两个物种。

【三要件】(全部 t 及之前无前视)
  A. 强势不妖:  近 strong_win 日涨幅 15%~60% (涨幅过大=已妖/透支)
  B. 横盘不跌:  近 flat_win 日 从最高点回撤 < max_drawdown
                + 量能不萎缩 (flat 均量 >= 前均量 × vol_floor)
  C. 再上攻:    近 spike_win 日出现放量阳线 (横盘后的突破苗头)

【妖股滤网】(剔除游资妖股)
  - 近20日涨幅 > max_rally  (一波流透支)
  - 近 limit_win 日 涨停次数 >= max_limit   (连板情绪票)
  - 日均换手率 > max_turnover               (对倒嫌疑, 需外部输入 turnover)
  - 近期出现连续跌停/天地板 (简易: 单日跌幅 > -15% 视为异动)

输出: momentum_score 0~100 + 状态 PLATEAU(横盘蓄势) / SURGE(再上攻确认)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12

PLATEAU = "PLATEAU"   # 横盘不跌 (核心候选, 等再上攻)
SURGE = "SURGE"       # 已出现放量上攻 (第二优先)
NONE = "NONE"


def momentum_features(close: pd.Series, high: pd.Series, low: pd.Series,
                      vol: pd.Series, turnover: pd.Series | None = None) -> pd.DataFrame:
    """计算动量矛全套特征 + 打分 + 状态"""
    out = pd.DataFrame(index=close.index)

    # ---- A. 强势不妖 ----
    ret_20 = close.pct_change(20)
    ret_10 = close.pct_change(10)
    # 近5日涨幅 (横盘期应收敛)
    ret_5 = close.pct_change(5)

    # ---- B. 横盘不跌 + 量能不缩 ----
    high_7 = high.rolling(7, min_periods=3).max().shift(1)   # 前7日最高 (含当日之前)
    drawdown = close / (high_7 + EPS) - 1.0                  # 距前高回撤 (负值)
    vol_7 = vol.rolling(7, min_periods=3).mean()
    vol_20 = vol.rolling(20, min_periods=5).mean()
    vol_ratio = vol_7 / (vol_20 + EPS)                        # >1 量能不缩

    # ---- C. 再上攻 ----
    # 近5日出现 放量阳线: 收阳且量 >= 20日均量×1.2
    up_day = (close.diff() > 0) & (vol >= vol_20 * 1.2)
    up_signal = up_day.rolling(5, min_periods=1).max().fillna(False).astype(bool)

    # ---- 妖股滤网 ----
    max_ret_20 = close.pct_change(20)
    # 涨停: 用 pct_chg 近似 (主板10% / 创业科创20%)
    pct_chg = close.pct_change()
    limit_10 = pct_chg >= 0.098
    limit_20 = pct_chg >= 0.195
    limit_count = (limit_10 | limit_20).rolling(10, min_periods=1).sum()
    # 异动大跌 (天地板/崩盘票)
    crash = (pct_chg <= -0.15).rolling(5, min_periods=1).max().fillna(False).astype(bool)
    if turnover is not None:
        turnover_ma = turnover.rolling(20, min_periods=5).mean()

    out["ret_20"] = ret_20
    out["ret_10"] = ret_10
    out["ret_5"] = ret_5
    out["drawdown"] = drawdown
    out["vol_ratio"] = vol_ratio
    out["up_signal"] = up_signal
    out["limit_count"] = limit_count
    out["crash"] = crash
    if turnover is not None:
        out["turnover"] = turnover_ma

    return out


def momentum_score(feat: pd.DataFrame, strong_win_lo: float = 0.15,
                   strong_win_hi: float = 0.60, max_drawdown: float = -0.08,
                   vol_floor: float = 0.90, max_rally: float = 0.70,
                   max_limit: int = 2, max_turnover: float = 15.0) -> pd.DataFrame:
    """在 momentum_features 输出上打分 + 状态判定, 返回带 momentum_score/momentum_state 的 DataFrame"""
    out = feat.copy()
    def _clip(s):
        return s.clip(0.0, 1.0)

    # A 强势不妖: 20日涨幅在 [0.15, 0.60] 内最佳 (倒V)
    a_rally = _clip((out["ret_20"] - strong_win_lo) / (0.20))          # 0.15起, 0.35满分
    a_rally = a_rally.where(out["ret_20"] <= 0.45, _clip(1.0 - (out["ret_20"] - 0.45) / 0.25))
    a_rally = a_rally.where(out["ret_20"] <= strong_win_hi, 0.0)       # >0.60 判妖
    # A 补充: 近5日不能暴涨 (横盘期收敛)
    a_flat = _clip(1.0 - out["ret_5"].abs() / 0.10)

    # B 横盘不跌: 回撤接近0最好 (越接近0=越横得住)
    b_hold = _clip((out["drawdown"] - max_drawdown) / (0 - max_drawdown))  # -0.08~0 线性
    # B 量能不缩: vol_ratio >= 0.9 不扣分, 越高越好
    b_vol = _clip((out["vol_ratio"] - vol_floor) / 0.8)

    # C 再上攻: 出现放量阳线给分
    c_signal = out["up_signal"].astype(float)

    # 妖股滤网 (硬条件, 命中即0分)
    filter_bad = (
        (out["ret_20"] > max_rally)          # 一波流透支
        | (out["limit_count"] >= max_limit)  # 连板情绪票
        | (out["crash"])                     # 异动崩盘
    )
    if "turnover" in out.columns:
        filter_bad = filter_bad | (out["turnover"] > max_turnover)  # 对倒嫌疑

    score = (0.35 * a_rally + 0.15 * a_flat
             + 0.25 * b_hold + 0.10 * b_vol
             + 0.15 * c_signal) * 100.0
    score = score.where(~filter_bad, 0.0)

    # 状态: 横盘不跌(回撤<8% 且 量能不缩) -> PLATEAU; 且出现放量上攻 -> SURGE
    state = pd.Series(NONE, index=out.index, dtype=object)
    is_plateau = (out["drawdown"] > max_drawdown) & (out["vol_ratio"] >= vol_floor) \
                 & (out["ret_20"] > strong_win_lo) & ~filter_bad
    is_surge = is_plateau & out["up_signal"]
    state = state.where(~is_plateau, PLATEAU)
    state = state.where(~is_surge, SURGE)
    out["momentum_score"] = score
    out["momentum_state"] = state
    return out


def is_momentum_candidate(feat: pd.DataFrame, min_score: float = 60.0) -> pd.Series:
    """动量矛候选: PLATEAU 或 SURGE 且 分数达标"""
    return (
        ((feat["momentum_state"] == PLATEAU) | (feat["momentum_state"] == SURGE))
        & (feat["momentum_score"] >= min_score)
    )