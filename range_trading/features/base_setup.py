# -*- encoding: utf-8 -*-
"""
蓄势启动识别器 (Base Breakout / Surge Setup Detector)

【定位】在长期下跌或底部区域, 识别"放量反弹 → 缩量消化 → 结构未破坏"的
主升浪/强势反弹蓄势形态, 目标是"启动前捕捉" (如紫金矿业 7.3-7.13 的 7 天整理)。

【三要件】(全部用 t 及之前数据, 无前视):
  A. 承接 (Accumulation): 近期出现过放量有力反弹
     - 近 N 日内 最大单日涨幅 / 累计反弹幅度 显著为正
     - 反弹日伴随放量 (资金承接, 非无量空涨)
  B. 消化 (Digestion): 反弹后缩量窄幅整理
     - 近 M 日振幅收窄 (高低点收敛)
     - 量能较反弹期明显萎缩 (浮筹被清洗)
     - 不放量阴跌 (缩量回调是洗盘, 放量下跌是派发)
  C. 结构完整 (Structure Intact): 守住反弹起点
     - 收盘不破反弹波段的低点 (或仅轻微刺破即收回)
     - 更高低点 (HL) 结构未破坏

【输出】BaseSetup 打分 0~100 + 状态标签:
  NONE / ACCUMULATING(承接中) / DIGESTING(消化中, ★候选) / LAUNCHING(启动)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12

# 状态标签
NONE = "NONE"
ACCUMULATING = "ACCUMULATING"   # 承接出现 (反弹中, 尚未消化)
DIGESTING = "DIGESTING"         # 消化中 (缩量整理, 核心候选)
LAUNCHING = "LAUNCHING"         # 启动 (放量突破消化区上沿)


# ---------------------------------------------------------------- 特征

def rally_strength(close: pd.Series, vol: pd.Series, window: int = 10) -> tuple:
    """
    承接强度 (要件A): 近 window 日的反弹力度与量能配合
      max_daily   近 window 日最大单日涨幅
      cum_rally   近 window 日自最低点的累计反弹幅度
      rally_vol   上涨日量 / 下跌日量 (>1 承接占优)
    """
    ret = close.pct_change()
    max_daily = ret.rolling(window, min_periods=3).max()
    low_n = close.rolling(window, min_periods=3).min()
    cum_rally = close / (low_n + EPS) - 1.0
    up_vol = vol.where(ret > 0, 0.0).rolling(window).sum()
    down_vol = vol.where(ret < 0, 0.0).rolling(window).sum()
    rally_vol = up_vol / (down_vol + EPS)
    return max_daily, cum_rally, rally_vol


def digestion_quality(high: pd.Series, low: pd.Series, close: pd.Series,
                      vol: pd.Series, window: int = 7) -> tuple:
    """
    消化质量 (要件B): 近 window 日的整理形态
      range_pct   近 window 日振幅 (High max - Low min) / Close  -- 越窄越好
      vol_shrink  近 window 日均量 / 前 window 日均量  -- <1 缩量
      tight_days  近 window 日内, 振幅 < 中位振幅的天数占比
    """
    hh = high.rolling(window, min_periods=2).max()
    ll = low.rolling(window, min_periods=2).min()
    range_pct = (hh - ll) / (close + EPS)
    vol_now = vol.rolling(window, min_periods=2).mean()
    vol_prev = vol.shift(window).rolling(window, min_periods=2).mean()
    vol_shrink = vol_now / (vol_prev + EPS)
    daily_rng = (high - low) / (close + EPS)
    med_rng = daily_rng.rolling(60, min_periods=20).median()
    tight_days = (daily_rng < med_rng).astype(float).rolling(window, min_periods=2).mean()
    return range_pct, vol_shrink, tight_days


def structure_intact(close: pd.Series, low: pd.Series, rally_lookback: int = 20) -> tuple:
    """
    结构完整 (要件C): 守住反弹起点 / 更高低点
      hold_low      收盘 > 近 rally_lookback 日最低 (反弹起点) 的 1.01 倍
      swing_low_up  最近的摆动低点高于前一个摆动低点 (HL 结构)
      dist_to_low   当前价距近 window 低点的距离 (越小越贴近支撑)
    """
    base_low = low.rolling(rally_lookback, min_periods=5).min()
    hold_low = close > base_low * 1.01
    dist_to_low = close / (base_low + EPS) - 1.0
    # HL: 近 10 日最低 > 前 10~20 日最低
    low_recent = low.rolling(10, min_periods=3).min()
    low_prev = low.shift(10).rolling(10, min_periods=3).min()
    swing_low_up = low_recent > low_prev
    return hold_low, swing_low_up, dist_to_low


def launch_confirm(close: pd.Series, high: pd.Series, vol: pd.Series,
                   digest_window: int = 7, vol_mult: float = 1.5) -> pd.Series:
    """启动确认: 放量突破消化区上沿 (近 digest_window 日最高)"""
    digest_high = high.rolling(digest_window, min_periods=2).max().shift(1)
    vol_ma = vol.rolling(20, min_periods=5).mean()
    return (close > digest_high) & (vol > vol_ma * vol_mult)


# ---------------------------------------------------------------- 综合打分

def base_setup_score(close: pd.Series, high: pd.Series, low: pd.Series,
                     vol: pd.Series) -> pd.DataFrame:
    """
    计算蓄势识别器全套特征 + 状态 + 打分 (返回 DataFrame, 与 close 对齐)。
    """
    out = pd.DataFrame(index=close.index)

    max_daily, cum_rally, rally_vol = rally_strength(close, vol, 10)
    range_pct, vol_shrink, tight_days = digestion_quality(high, low, close, vol, 7)
    hold_low, swing_low_up, dist_to_low = structure_intact(close, low, 20)
    launch = launch_confirm(close, high, vol)

    # ---- 结构土壤 (识别"有空间的启动起点", 放宽is_base) ----
    # 旧逻辑: 必须深跌 ret120<-15% (把横盘/平台突破误杀)。
    # 新逻辑: "非高位"即合格 —— 排除已大涨后的高位 (ret120>+30% 或 收盘远超120日高点),
    #         其余 (深跌底部 / 长期横盘平台 / 横盘后平台突破) 都是允许的启动土壤。
    ret_120 = close.pct_change(120)
    high_120 = high.rolling(120, min_periods=60).max()
    pos_h120 = close / (high_120 + EPS) - 1.0       # 当前价相对 120日高点的位置 (<0 = 未创新高)
    not_high = (ret_120 < 0.30) & (pos_h120 < -0.03)  # 未大涨 + 未贴近120日高点
    low_120 = low.rolling(120, min_periods=60).min()
    not_new_low = close >= low_120 * 0.99           # 未有效跌破 120 日低点
    is_base = not_high & not_new_low

    # ---- 涨停识别 (保留涨停启动标的) ----
    # A股涨停幅度: 主板 10% / 创业板科创板 20%。涨停日放量上攻 = 强承接, 不判晚。
    pct_chg = close.pct_change()
    # 用前收判断涨停 (近似: 主板≥9.8%, 创业/科创≥19.5%; 无板块信息时按 9.8% 兜底)
    limit_up_10 = pct_chg >= 0.098
    limit_up_20 = pct_chg >= 0.195
    # 近 10 日出现过涨停 (涨停启动的土壤)
    limit_up_any = (limit_up_10 | limit_up_20).rolling(10, min_periods=1).max().fillna(False).astype(bool)

    out["max_daily"] = max_daily
    out["cum_rally"] = cum_rally
    out["rally_vol"] = rally_vol
    out["range_pct"] = range_pct
    out["vol_shrink"] = vol_shrink
    out["tight_days"] = tight_days
    out["hold_low"] = hold_low
    out["swing_low_up"] = swing_low_up
    out["dist_to_low"] = dist_to_low
    out["launch"] = launch
    out["is_base"] = is_base
    out["ret_120"] = ret_120
    out["pos_h120"] = pos_h120
    out["limit_up_10"] = limit_up_10
    out["limit_up_20"] = limit_up_20
    out["limit_up_any"] = limit_up_any

    def _clip(s, lo=0.0, hi=1.0):
        return s.clip(lo, hi)

    # ---- 要件得分 (0~1) ----
    # A 承接: 反弹幅度 + 反弹量能 + 涨停启动加分
    a_rally = _clip(cum_rally / 0.10)                 # 累计反弹 10% 满分
    a_vol = _clip((rally_vol - 1.0) / 1.0)            # 上涨日量占优
    # 涨停启动 = 强承接证据, 不因涨得多而判晚, 反而加分 (涨停说明主力资金强)
    a_limit = limit_up_any.astype(float) * 0.5
    score_a = _clip(0.5 * a_rally + 0.3 * a_vol + 0.2 * a_limit)

    # B 消化: 收敛 + 缩量 + 多日紧凑
    b_narrow = _clip(1.0 - range_pct / 0.18)          # 7日振幅 <=18% 满分
    b_shrink = _clip((1.0 - vol_shrink) / 0.5 + 0.0)  # vol_shrink 越小越好 (<=0.5 满分)
    b_tight = _clip(tight_days / 0.6)                 # >=60% 紧凑日
    score_b = _clip(0.35 * b_narrow + 0.45 * b_shrink + 0.20 * b_tight)

    # C 结构: 守住低点 + HL 完整
    c_hold = hold_low.astype(float)
    c_hl = swing_low_up.astype(float)
    score_c = _clip(0.6 * c_hold + 0.4 * c_hl)

    out["score_a"] = score_a * 100.0
    out["score_b"] = score_b * 100.0
    out["score_c"] = score_c * 100.0

    # ---- 综合蓄势分 ----
    out["base_score"] = _clip(0.35 * score_a + 0.40 * score_b + 0.25 * score_c) * 100.0

    # ---- 状态判定 (只做消化期: 追启动日已被回测证伪, MAE -13.8% 负期望) ----
    # 注意 where 语义: s.where(cond, other) = cond为True保留s, 否则取other。
    # 因此"把 x 处设为 DIGESTING"应写 state.where(~(x), DIGESTING)。
    state = pd.Series(NONE, index=close.index, dtype=object)
    has_rally = (cum_rally > 0.05) & (rally_vol > 1.0)          # 有承接
    # 消化: 收敛 + 缩量 + 守低点; 涨停启动票放宽反弹幅度上限 (涨停本身即消化后启动)
    cum_rally_ok = (cum_rally < 0.40) | limit_up_any
    is_digest = (range_pct < 0.18) & (vol_shrink < 1.0) & hold_low & cum_rally_ok

    state = state.where(~(has_rally & is_digest), DIGESTING)     # 消化中 -> DIGESTING
    state = state.where(~(has_rally & ~is_digest), ACCUMULATING)  # 有承接但未消化 -> 承接中
    out["setup_state"] = state
    return out


def is_candidate(setup: pd.DataFrame, min_score: float = 60.0,
                 max_score: float = 1e9, require_base: bool = True) -> pd.Series:
    """
    候选判定: 消化中 (DIGESTING), 蓄势分在 [min_score, max_score] 区间,
    且 (require_base) 处于底部土壤。
    - 只做 DIGESTING (追 LAUNCHING 已被回测证伪: 放量日介入 MAE -13.8%, 负期望);
    - 分数区间制: 过高 (>max_score) 说明已晚, 过滤过热票。
    """
    base_ok = setup["is_base"].fillna(False) if require_base else True
    return (
        (setup["setup_state"] == DIGESTING)
        & (setup["base_score"] >= min_score)
        & (setup["base_score"] < max_score)
        & base_ok
    )
