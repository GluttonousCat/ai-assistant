# -*- encoding: utf-8 -*-
"""
趋势打分: VPA 量价共振矩阵 + TQS (Trend Quality Score) 五分项 + Stage 检测

结构 (趋势指南第八章, 权重对偶震荡系统):
    TQS = 0.30 Efficiency + 0.25 VolumePrice + 0.20 Structure
        + 0.15 Sustainability + 0.10 Stage

【工程铁律】(趋势指南 5.6): 价格形态再好, VPA 不达标就不入场。
VPA 是独立的硬门槛, 与 TQS 并列输出。

所有分项输出 0~100; 阈值/权重全部来自 TrendConfig (趋势指南第五十二章原则)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from range_trading.config import TrendConfig, DEFAULT_TREND_CONFIG


def _clip01(s: pd.Series) -> pd.Series:
    return s.clip(lower=0.0, upper=1.0)


def compute_vpa(feat: pd.DataFrame,
                config: TrendConfig | None = None) -> pd.Series:
    """
    VPA 量价共振矩阵 (趋势指南 5.6), 五层证据加权, 输出 0~1。

    子项 (均归一到 [0,1]):
      breakout_volume  突破日量比 (vol_ratio / breakout_vol_ratio)
      obv_sync         价格-OBV 同步 (obv_confirm 直接 ∈ {0,0.5,1})
      udvol            上涨日量占比 (udvol / udvol_full)
      cmf              资金流方向 (cmf / cmf_full)
      vwap_support     价格在 VWAP 上方 且 重心斜率同向
    """
    cfg = config or DEFAULT_TREND_CONFIG
    w = cfg.vpa_weights

    breakout_volume = _clip01(feat["vol_ratio"].fillna(0.0) / cfg.breakout_vol_ratio)
    obv_sync = feat["obv_confirm"].fillna(0.0)                    # ∈ {0, 0.5, 1}
    udvol = _clip01(feat["udvol"].fillna(0.0) / cfg.udvol_full)
    cmf = _clip01(feat["cmf"].fillna(0.0) / cfg.cmf_full)
    # VWAP 支撑: 价格在 VWAP 上方 (vwap_dev_pct > 0) 且重心抬升 (vwap_slope > 0)
    above = (feat["vwap_dev_pct"] > 0).astype(float)
    slope_up = _clip01(feat["vwap_slope"].fillna(0.0) / cfg.vwap_slope_full)
    vwap_support = (above * 0.5 + slope_up * 0.5)

    vpa = (
        w["breakout_volume"] * breakout_volume
        + w["obv_sync"] * obv_sync
        + w["udvol"] * udvol
        + w["cmf"] * cmf
        + w["vwap_support"] * vwap_support
    )
    return vpa.clip(0.0, 1.0)


def detect_stage(feat: pd.DataFrame,
                 config: TrendConfig | None = None) -> pd.Series:
    """
    趋势阶段检测 (趋势指南第七章, Weinstein 四阶段简化):
        Stage 1 积累: 价格在长均线下方/走平, ER 低
        Stage 2 上升: 价格在长均线上方, ER 高  ★唯一做多区
        Stage 3 派发: 价格在长均线上方, 但 ER 回落 + 量价背离
        Stage 4 下降: 价格在长均线下方, ER 高 (向下趋势)
    输出整数列 1/2/3/4。
    """
    cfg = config or DEFAULT_TREND_CONFIG
    above = feat["above_ma"].fillna(False)
    er = feat[f"er_{cfg.stage_er_window}"]
    bear_div = feat["bear_div"].fillna(False)

    stage = pd.Series(1, index=feat.index)
    stage = stage.where(~(above & (er > cfg.stage_er_threshold)), 2)
    # Stage 3: 在均线上方但效率回落 + 出现顶背离
    stage = stage.where(~(above & (er < cfg.stage_er_flat) & bear_div), 3)
    # Stage 4: 均线下方且效率高 (向下)
    stage = stage.where(~(~above & (er > cfg.stage_er_threshold)), 4)
    return stage


def score_trend(feat: pd.DataFrame,
                config: TrendConfig | None = None) -> pd.DataFrame:
    """在趋势特征宽表上计算 TQS 五分项 + VPA + Stage, 返回打分列"""
    cfg = config or DEFAULT_TREND_CONFIG
    w = cfg.tqs_weights
    out = pd.DataFrame(index=feat.index)

    def f(name: str) -> pd.Series:
        return feat[name]

    er20 = f("er_20")

    # ---------- 1. Efficiency: 方向效率回归 ----------
    er_level = _clip01(er20 / cfg.er_confirm)
    er_slope = _clip01(f("er_slope").fillna(0.0) / cfg.er_slope_full)
    net = _clip01(f("net_slope").fillna(0.0) / cfg.net_slope_full)
    out["eff_score"] = (0.5 * er_level + 0.3 * er_slope + 0.2 * net) * 100.0

    # ---------- 2. VolumePrice: VPA 共振矩阵 ----------
    out["vpa"] = compute_vpa(feat, cfg)
    out["vp_score"] = out["vpa"] * 100.0

    # ---------- 3. Structure: 结构完整 + 回调质量 ----------
    integrity = _clip01(f("integrity").fillna(0.0) / 0.75)
    # 回调越浅越好 (depth 0=未回调在高位, 0.4=健康, >0.6 差)
    depth = f("pullback_depth").fillna(1.0)
    pullback_q = _clip01(1.0 - depth / cfg.pullback_soft)
    above_vwap = (f("vwap_dev_pct") > 0).astype(float)
    out["struct_score"] = (0.5 * integrity + 0.3 * pullback_q + 0.2 * above_vwap) * 100.0

    # ---------- 4. Sustainability: 量能持续(非脉冲) + 回调缩量 + 资金流入 ----------
    persist = _clip01(f("vol_persist").fillna(0.0) / cfg.vol_persist_full)
    no_pulse = _clip01(1.0 - f("pulse_share").fillna(1.0) / 0.6)  # 脉冲占比越低越好
    shrink_pull = _clip01(1.0 - f("pullback_vol").fillna(1.0) / 1.0)
    cmf_pos = _clip01(f("cmf").fillna(0.0) / cfg.cmf_full)
    out["sust_score"] = (0.35 * persist + 0.25 * no_pulse
                         + 0.20 * shrink_pull + 0.20 * cmf_pos) * 100.0

    # ---------- 5. Stage: 阶段加分 ----------
    stage = detect_stage(feat, cfg)
    out["stage"] = stage
    # Stage 2 = 100, Stage 1 = 40 (可能即将进入), Stage 3 = 20 (派发), Stage 4 = 0
    stage_map = {2: 100.0, 1: 40.0, 3: 20.0, 4: 0.0}
    out["stage_score"] = stage.map(stage_map).fillna(0.0)

    # ---------- 合成 TQS ----------
    base = (
        w["efficiency"] * out["eff_score"]
        + w["volume_price"] * out["vp_score"]
        + w["structure"] * out["struct_score"]
        + w["sustainability"] * out["sust_score"]
        + w["stage"] * out["stage_score"]
    )

    # ---------- 衰竭扣减 (趋势指南第十一章): 背离 + ER回落 + HL破坏 ----------
    er_fall = _clip01((f("er_peak") - er20).fillna(0.0) / cfg.er_fall_amount)
    divergence = f("bear_div").fillna(False).astype(float)
    hl_break = f("hl_break").fillna(False).astype(float)
    # 衰竭证据计数 (0~3), 每项 1/3 权重
    exhaust_evidence = (divergence + er_fall + hl_break) / 3.0
    out["exhaust_evidence"] = exhaust_evidence * 100.0
    out["er_fall"] = er_fall
    # bear_div / hl_break / false_break 已在特征层输出, 此处不重复 (避免 join 列冲突)

    out["tqs"] = (base - 0.30 * out["exhaust_evidence"]).clip(0.0, 100.0)

    # 假突破降权 (趋势指南 6.3): 命中 false_break 的突破日 TQS 打折
    false_break = f("false_break").fillna(False)
    out["tqs"] = out["tqs"].where(~false_break, out["tqs"] * 0.5)

    # ---------- 游资脉冲硬扣分 (指南第十八章陷阱3) ----------
    # pulse_share > pulse_hard: 单日贡献过半量能 -> 脉冲非持续, 后续必然枯竭
    # 超阈值部分线性映射到 [0, pulse_penalty_full] 比例扣减 TQS
    # (pulse_share 由特征层输出, 此处不重复, 避免 join 列冲突)
    pulse = f("pulse_share").fillna(0.0)
    pulse_over = _clip01((pulse - cfg.pulse_hard) / (1.0 - cfg.pulse_hard))
    out["pulse_penalty"] = pulse_over * cfg.pulse_penalty_full * 100.0
    out["tqs"] = (out["tqs"] - out["pulse_penalty"]).clip(0.0, 100.0)

    # ---------- 趋势年龄 (自最近 swing_low 起) ----------
    out["trend_late"] = f("bear_div") & (er20 < cfg.stage_er_flat)

    return out


def trend_stage_label(stage: float) -> str:
    return {1: "积累", 2: "上升", 3: "派发", 4: "下降"}.get(int(stage), "未知")
