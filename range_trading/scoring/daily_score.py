"""
日K打分: DailyRangeScore / DailyRangeProbability / TrendRisk / Bias

结构 (指南第二十章, 权重适配日K):
    DailyRangeScore = 0.30 TrendFailure
                    + 0.25 Inefficiency
                    + 0.20 RangeFormation
                    + 0.15 BoundaryEvidence
                    + 0.10 VolatilityQuality

并按指南第三十八节做假阳性防护:
    RangeProbability = TransitionEvidence - TrendContinuationEvidence
其中 TrendContinuationEvidence 由 HH/HL 率、净位移斜率、长周期 DI 构成,
作为扣减项而非独立否决 (Trend Pullback Filter / 高波动趋势过滤, 第三十七~三十九节)。

所有分项输出 0~100; 全部阈值/权重来自 DailyConfig。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from range_trading.config import DailyConfig, DEFAULT_DAILY_CONFIG


def _clip01(s: pd.Series) -> pd.Series:
    return s.clip(lower=0.0, upper=1.0)


def score_daily(feat: pd.DataFrame,
                config: DailyConfig | None = None) -> pd.DataFrame:
    """在特征宽表上计算五分项得分与合成分数, 返回仅含打分列的 DataFrame"""
    cfg = config or DEFAULT_DAILY_CONFIG
    w = cfg.score_weights
    sw = cfg.sub_weights
    out = pd.DataFrame(index=feat.index)

    def f(name: str) -> pd.Series:
        return feat[name]

    # ---------- 1. TrendFailure: 趋势正在失效 ----------
    tf_parts = {
        "di_slope": _clip01(f("di_slope") / cfg.di_slope_full),
        "di_accel": _clip01(f("di_accel") / cfg.di_accel_full),
        # |EMA20 斜率| 相对 k 天前的下降幅度 -> 斜率趋零 (指南 15.2B)
        "slope_decay": _clip01(
            (f("ema20_slope").abs().shift(cfg.di_slope_k) - f("ema20_slope").abs())
            / cfg.ema_slope_decay_ref
        ),
    }
    out["tf_score"] = sum(
        tf_parts[k] * sw["trend_failure"][k] for k in tf_parts
    ) * 100.0

    # ---------- 2. Inefficiency: DI + FlipRate + AC1 组合证据 ----------
    di_level = pd.concat(
        [feat[f"di_{d}"] for d in cfg.di_windows[1:]], axis=1
    ).mean(axis=1)
    flip_level = pd.concat(
        [feat[f"flip_{d}"] for d in cfg.flip_windows], axis=1
    ).mean(axis=1)
    ineff_parts = {
        "di_level": di_level,
        "flip": _clip01(flip_level.fillna(0.0) / cfg.flip_full),
        # 收益零方差时 corr 无定义 (NaN), 语义为 "无反向证据" -> 记 0 分
        "ac1": _clip01(-f("ac1").fillna(0.0) / cfg.ac1_full),
    }
    out["ineff_score"] = sum(
        ineff_parts[k] * sw["inefficiency"][k] for k in ineff_parts
    ) * 100.0

    # ---------- 3. RangeFormation: 边界稳定 + 宽度稳定 + 价格密度 ----------
    rf_parts = {
        "upper_stab": f("up_stab"),
        "lower_stab": f("lo_stab"),
        "width_stab": f("width_stab"),
        "density": f("density"),
    }
    out["rf_score"] = sum(
        rf_parts[k] * sw["range_formation"][k] for k in rf_parts
    ) * 100.0

    # ---------- 4. BoundaryEvidence: 触碰 + 反应 ----------
    # 无触碰 -> 无反应证据, 记 0 分而非 NaN (否则趋势段合成分会整体失效)
    rejection = (
        _clip01(f("sup_resp").fillna(0.0) / cfg.reaction_full_atr)
        + _clip01(f("res_resp").fillna(0.0) / cfg.reaction_full_atr)
    ) / 2.0
    be_parts = {
        "touch": _clip01(f("touch").fillna(0.0) / cfg.touch_full),
        "rejection": rejection,
    }
    out["be_score"] = sum(
        be_parts[k] * sw["boundary_evidence"][k] for k in be_parts
    ) * 100.0

    # ---------- 5. VolatilityQuality: NATR 分位不低 + ATR 稳定 ----------
    vq_parts = {
        "natr_pct": _clip01(
            (f("natr_pct") - cfg.natr_floor) / (cfg.natr_ceil - cfg.natr_floor)
        ),
        "atr_stab": f("atr_stab"),
    }
    out["vq_score"] = sum(
        vq_parts[k] * sw["volatility_quality"][k] for k in vq_parts
    ) * 100.0

    # ---------- 合成 ----------
    base = (
        w["trend_failure"] * out["tf_score"]
        + w["inefficiency"] * out["ineff_score"]
        + w["range_formation"] * out["rf_score"]
        + w["boundary_evidence"] * out["be_score"]
        + w["volatility_quality"] * out["vq_score"]
    )

    # ---------- TrendContinuationEvidence (假阳性扣减) ----------
    hh_hl = (f("hh_rate") + f("hl_rate")) / 2.0
    di_long = feat[f"di_{cfg.di_windows[-1]}"]
    tc_parts = {
        # HH/HL 双高: 更可能是 Trend Pullback 而非 Range Formation (指南第三十八节)
        "hh_hl": _clip01((hh_hl - cfg.hh_hl_floor) / (cfg.hh_hl_full - cfg.hh_hl_floor)),
        # 净位移持续存在: 高波动趋势 (指南第三十九节)
        "net_slope": _clip01(f("net_slope").abs() / cfg.net_slope_ref),
        # 长周期 DI 仍低: 原趋势未真正失效
        "di_long": _clip01((cfg.di_long_floor - di_long) / cfg.di_long_floor),
    }
    out["trend_cont"] = sum(
        tc_parts[k] * sw["trend_continuation"][k] for k in tc_parts
    ) * 100.0

    # ---------- 区间宽度约束 (指南第三十四节): 过宽区间扣减 ----------
    width = f("range_width")
    width_penalty = _clip01(
        (width - cfg.width_ideal_max) / (cfg.width_hard_max - cfg.width_ideal_max)
    )
    out["width_penalty"] = width_penalty * cfg.width_penalty_weight * 100.0

    out["daily_range_score"] = (
        base
        - cfg.trend_penalty_weight * out["trend_cont"]
        - out["width_penalty"]
    ).clip(lower=0.0, upper=100.0)

    # ---------- 16章输出: DailyRangeProbability ----------
    out["range_prob"] = out["daily_range_score"] / 100.0

    # ---------- 31章输出: 偏置 / 趋势风险 / 区间兼容性 ----------
    out["long_bias"] = (
        _clip01(f("ema60_slope") / cfg.bias_ref) * 2.0 - 1.0
    ) * 100.0
    out["short_bias"] = -out["long_bias"]
    out["trend_risk_score"] = out["trend_cont"]
    out["range_compat"] = (100.0 - out["trend_cont"]).clip(lower=0.0, upper=100.0)

    return out


def trend_risk_label(score: float) -> str:
    """TrendRisk 分级: LOW / MEDIUM / HIGH"""
    if score is None or np.isnan(score):
        return "NA"
    if score < 35.0:
        return "LOW"
    if score < 60.0:
        return "MEDIUM"
    return "HIGH"
