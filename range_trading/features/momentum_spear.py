# -*- encoding: utf-8 -*-
"""
动量矛 (Momentum Spear): 识别"科技/成长主升浪"中的强势平台整理

【定位】与 base_setup(蓝筹蓄势盾) 互补。base_setup 抓"底部反弹+缩量消化"的温和波段,
动量矛抓"放量点火 → 横盘不跌 → 量能不缩 → 再上攻"的科技主升浪 (如 4.8 启动的光通信/PCB/芯片)。

【形态内核】(用户洞察: 科技大涨几天→横盘几天→不下跌)
- 大涨 3 天:  近 N 日出现放量长阳 (单日涨幅显著 + 放量) 或累计涨幅显著
- 横盘不跌:   近 7 日从高点回撤 < 8% (只是横盘, 没下跌)
- 量能不缩:   近 7 日均量 >= 近20日均量 × 0.9 (科技牛的核心: 量在承接在)
- 已启动未透支: 近20日涨幅 10%~70%

【妖股滤网】(避免抓游资妖股)
- 近20日涨幅 > 70%  → 已妖/透支
- 近5日涨停数 >= 2  → 游资连板 (情绪票)
- 市值 < 30亿        → 微盘游资
- 日均换手 > 15%     → 对倒嫌疑 (在扫描层用 daily_basic 过滤, 本模块输出换手度)

输出 0~100 动量分 + 状态 (NONE/IGNITED点火/PLATEAU平台/READY待突破)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12

NONE = "NONE"
IGNITED = "IGNITED"       # 刚放量点火
PLATEAU = "PLATEAU"       # 点火后横盘不跌 (核心候选)
READY = "READY"           # 横盘后再上攻确认

# 阈值 (可由 config 覆盖)
DD7_MAX = 0.08            # 近7日回撤上限 (横盘不跌)
VOL_KEEP = 0.90           # 7日均量/20日均量 下限 (量能不缩)
RET20_MIN = 0.10          # 近20日涨幅下限 (已启动)
RET20_MAX = 0.70          # 近20日涨幅上限 (未透支, 滤妖)
IGNITE_DAY = 0.06         # 点火日单日涨幅 (放量长阳)
IGNITE_VOL = 1.5          # 点火日量比
LIMIT_5 = 2               # 近5日涨停数上限 (滤连板妖股)
BODY_MIN = 0.03           # 放量阳线实体最低涨幅 (再上攻确认)


def momentum_features(close: pd.Series, high: pd.Series, low: pd.Series,
                      vol: pd.Series, amount: pd.Series | None = None) -> pd.DataFrame:
    """
    动量矛全套特征 (与 close 对齐, 只用 t 及之前数据)。

    需要 amount (成交额) 计算换手参考; 若无则换手相关输出 NaN (妖股滤网退化为市值+涨停过滤)。
    """
    out = pd.DataFrame(index=close.index)

    # ---- 动量: 自20日低点起涨幅度 (启动动量, 排除固定窗口里前期洗盘干扰) ----
    low20 = low.rolling(20, min_periods=5).min()
    rally20 = close / (low20 + EPS) - 1.0      # 自20日低点起涨幅度 (启动动量)
    ret20 = close.pct_change(20)               # 固定20日涨幅 (妖股滤网参考: 透支度)
    hh7 = high.rolling(7, min_periods=4).max()
    dd7 = close / (hh7 + EPS) - 1.0          # 近7日回撤 (负值, 越大越不跌)
    ret5 = close.pct_change(5)
    ret1 = close.pct_change(1)
    ret3 = close.pct_change(3)

    # ---- 放量 ----
    vol20 = vol.rolling(20, min_periods=5).mean()
    vol7 = vol.rolling(7, min_periods=4).mean()
    vol_ratio = vol7 / (vol20 + EPS)         # 近7日量能 vs 20日 (量能不缩 >0.9)
    vol1_ratio = vol / (vol20 + EPS)         # 当日量比

    # 点火日: 单日大涨 + 放量 (放量长阳)
    ignite = (ret1 >= IGNITE_DAY) & (vol1_ratio >= IGNITE_VOL)
    ignite_any5 = ignite.rolling(5, min_periods=1).max().fillna(False).astype(bool)

    # 涨停数 (近5日): 主板 9.8%, 创业/科创 19.5%
    limit_up = (ret1 >= 0.098) | (ret1 >= 0.195)
    limit5 = limit_up.rolling(5, min_periods=1).sum()

    # 放量阳线 (再上攻确认): 当日涨 + 实体大 + 放量
    o = close.shift(1)                       # 近似开盘 (用昨收)
    body = (close - o) / (o + EPS)
    strong_up = (ret1 >= BODY_MIN) & (vol1_ratio >= 1.2)
    strong_up_any3 = strong_up.rolling(3, min_periods=1).max().fillna(False).astype(bool)

    out["ret20"] = ret20
    out["dd7"] = dd7
    out["ret5"] = ret5
    out["ret3"] = ret3
    out["vol_ratio"] = vol_ratio
    out["vol1_ratio"] = vol1_ratio
    out["ignite"] = ignite
    out["ignite_any5"] = ignite_any5
    out["limit5"] = limit5
    out["strong_up_any3"] = strong_up_any3

    # ---- 打分 (0~100) ----
    def _clip(s, lo=0.0, hi=1.0):
        return s.clip(lo, hi)

    # A 启动动量已出现但未透支: rally20(自低点起涨) 15%~70% 最佳; ret20>70% 透支
    a_start = _clip((rally20 - 0.15) / 0.10)
    a_ok = _clip(1.0 - (ret20 - RET20_MAX).clip(lower=0) / 0.30)
    score_a = _clip(0.7 * a_start + 0.3 * a_ok)

    # B 横盘不跌: dd7 越接近 0 越好 (回撤小)
    score_b = _clip(1.0 - (-dd7).clip(lower=0) / DD7_MAX)

    # C 量能不缩: vol_ratio >= VOL_KEEP 满分
    score_c = _clip((vol_ratio - 0.7) / (VOL_KEEP - 0.7))

    # D 再上攻确认: 点火后 5日内有强阳
    score_d = strong_up_any3.astype(float)

    # 涨停连板惩罚 (妖股): 近5日 >= LIMIT_5 个涨停, 大幅扣分
    limit_penalty = _clip((limit5 - (LIMIT_5 - 1)) / 2.0)

    out["score_a"] = score_a * 100
    out["score_b"] = score_b * 100
    out["score_c"] = score_c * 100
    out["score_d"] = score_d * 100
    out["momentum_score"] = _clip(
        0.30 * score_a + 0.25 * score_b + 0.25 * score_c + 0.20 * score_d
        - 0.5 * limit_penalty) * 100

    # ---- 状态 ----
    state = pd.Series(NONE, index=close.index, dtype=object)
    has_momentum = (rally20 >= 0.10) & (ret20 <= RET20_MAX)
    is_plateau = has_momentum & (-dd7 < DD7_MAX) & (vol_ratio >= VOL_KEEP)
    is_ready = is_plateau & strong_up_any3
    is_ignited = has_momentum & ignite_any5 & ~is_plateau

    state = state.where(~(is_ready), READY)
    state = state.where(~(is_plateau & ~is_ready), PLATEAU)
    state = state.where(~(is_ignited), IGNITED)
    out["momentum_state"] = state
    out["rally20"] = rally20

    # 妖股滤网标记 (扫描层再结合市值/换手)
    out["is_absurd"] = (ret20 > RET20_MAX) | (limit5 >= LIMIT_5)
    return out


def is_momentum_candidate(setup: pd.DataFrame, min_score: float = 60.0,
                          tech_only: bool = False) -> pd.Series:
    """动量矛候选: 平台整理 (PLATEAU/READY) 且动量分达标, 且非妖股。
    tech_only=True: 仅保留核心科技 (电子/通信/计算机/电力设备), 过滤非科技杂票。
    注意: 需在 setup 中带 l1 行业列 (由扫描层注入, 无则 tech_only 不生效)。"""
    base_ok = (
        (setup["momentum_state"].isin([PLATEAU, READY]))
        & (setup["momentum_score"] >= min_score)
        & (~setup["is_absurd"])
    )
    if tech_only and "l1" in setup.columns:
        base_ok = base_ok & setup["l1"].isin(TECH_L1)
    return base_ok


# ---- 行业权重 (科技成长板块加权, 其余降权) ----
# 锚定样本: 光通信/PCB/芯片核心股 全部落于 L1=电子 + L1=通信。
# 权重逻辑: 电子/通信 是主升浪核心 → 高分; 相关成长(计算机/电力设备) 中等;
#           传统板块 (消费/公用/银行/周期) 的"动量"多为杂票 → 强降权。
TECH_L1 = {"电子", "通信", "计算机", "电力设备"}
CORE_L1 = {"电子", "通信"}          # 核心科技 (光通信/PCB/半导体)
WEAK_L1 = {"银行", "公用事业", "食品饮料", "家用电器", "煤炭", "石油石化", "钢铁",
           "交通运输", "农林牧渔", "建筑装饰", "建筑材料", "纺织服饰", "美容护理", "环保"}

# 行业权重: 核心×1.35, 次核心×1.1, 传统×0.5, 默认×0.8
def industry_weight(l1: str) -> float:
    if l1 in CORE_L1:
        return 1.35
    if l1 in TECH_L1:
        return 1.10
    if l1 in WEAK_L1:
        return 0.50
    return 0.80


def apply_industry_weight(score: pd.Series, l1: str) -> pd.Series:
    """给动量分乘行业权重 (核心科技放大, 传统板块压制)"""
    return score * industry_weight(l1)