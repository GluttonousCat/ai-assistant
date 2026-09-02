"""
日K特征聚合入口

输入: 含 open/high/low/close (前复权) + vol/amount 列、按 trade_date 升序的 DataFrame;
输出: 逐日特征宽表 (pd.DataFrame, 与输入等长等 index, 只含 t 及之前信息)。

列命名约定:
    di_{w} / flip_{w} / up_{w} / lo_{w}  -- 按窗口展开
    ac1 / atr / natr / natr_pct / atr_stab
    range_pos / range_width / up_stab / lo_stab / width_stab / density / touch
    sup_resp / res_resp                 -- 边界反应 (ATR 倍数)
    ema{w}_slope / net_slope / hh_rate / hl_rate
    body_ratio / expansion / bull_event / bear_event / false_bd / false_bo
"""
from __future__ import annotations

import pandas as pd

from range_trading.config import DailyConfig, DEFAULT_DAILY_CONFIG, TrendConfig, DEFAULT_TREND_CONFIG
from range_trading.features import (
    accumulation, boundary, breakout, candle, directional, structural_context,
    structure, volatility, volume,
)

__all__ = [
    "compute_daily_features",
    "compute_trend_features",
    "accumulation", "boundary", "breakout", "candle", "directional",
    "structural_context", "structure", "volatility", "volume",
]


def compute_daily_features(df: pd.DataFrame,
                           config: DailyConfig | None = None) -> pd.DataFrame:
    """计算日K层全部特征, 返回特征宽表 (输入 df 需含 open/high/low/close/vol/amount)"""
    cfg = config or DEFAULT_DAILY_CONFIG
    out = pd.DataFrame(index=df.index)

    o, h, l, c = df["open"], df["high"], df["low"], df["close"]

    # ---- 方向效率: 多尺度 DI + 斜率 + 加速度 (主尺度 = di_windows[1], 即 20D) ----
    di_main = None
    for i, w in enumerate(cfg.di_windows):
        di_w = directional.directional_inefficiency(c, w)
        out[f"di_{w}"] = di_w
        if i == 1:
            di_main = di_w
    out["di_slope"] = directional.di_slope(di_main, cfg.di_slope_k)
    out["di_accel"] = directional.di_acceleration(di_main, cfg.di_slope_k)

    # ---- 反向运动频率 / 自相关 ----
    for w in cfg.flip_windows:
        out[f"flip_{w}"] = directional.flip_rate(c, w)
    out["ac1"] = directional.autocorr1(c, cfg.ac1_window)

    # ---- 波动 ----
    out["atr"] = volatility.atr(h, l, c, cfg.atr_window)
    out["natr"] = volatility.natr(h, l, c, cfg.atr_window)
    out["natr_pct"] = volatility.rolling_percentile(out["natr"], cfg.natr_pct_window)
    out["atr_stab"] = volatility.atr_stability(out["atr"], cfg.atr_stab_window)

    # ---- 价格边界: 多尺度 quantile 边界, 主窗口展开派生指标 ----
    for w in cfg.boundary_windows:
        up_w, lo_w = boundary.quantile_boundary(c, w, cfg.upper_quantile, cfg.lower_quantile)
        out[f"up_{w}"] = up_w
        out[f"lo_{w}"] = lo_w
    pw = cfg.boundary_primary
    up, lo = out[f"up_{pw}"], out[f"lo_{pw}"]
    out["range_pos"] = boundary.range_pos(c, up, lo)
    out["range_width"] = boundary.range_width(up, lo)
    out["up_stab"], out["lo_stab"] = boundary.boundary_stability(
        up, lo, cfg.boundary_slope_k, cfg.boundary_slope_ref)
    out["width_stab"] = boundary.width_stability(
        out["range_width"], cfg.boundary_slope_k, cfg.width_slope_ref)
    out["density"] = boundary.price_density(c, up, lo, cfg.density_window)
    out["touch"] = boundary.touch_count(h, l, up, lo, cfg.touch_window, cfg.touch_band)

    # ---- 量能 (辅助证据): 量比 / 分位 / OBV 斜率 / VWAP 重心 ----
    vol_ratio = volume.volume_ratio(df["vol"], cfg.volume_ma_window)
    out["vol_ratio"] = vol_ratio
    out["vol_pct"] = volume.volume_pct(df["vol"], cfg.volume_ma_window,
                                       cfg.volume_pct_window)
    out["obv_slope"] = volume.obv_slope(c, df["vol"], cfg.obv_slope_k, cfg.obv_slope_k)
    out["vwap_dev"] = volume.vwap_dev(h, l, c, df["vol"], up, lo, cfg.vwap_window)

    # 边界反应: 触碰日量比加权 (放量触碰的反转证据更可信)
    out["sup_resp"], out["res_resp"] = boundary.boundary_rejection(
        h, l, c, out["atr"], up, lo,
        cfg.touch_band, cfg.reaction_horizon, cfg.reaction_window,
        volume_ratio=vol_ratio)

    # ---- 趋势结构 / 假阳性过滤 ----
    for w in cfg.ema_windows:
        out[f"ema{w}_slope"] = structure.ema_slope(c, w, cfg.ema_slope_k)
    out["net_slope"] = structure.net_slope(c, cfg.net_slope_window)
    out["hh_rate"] = structure.higher_high_rate(h, cfg.hh_hl_window)
    out["hl_rate"] = structure.higher_low_rate(l, cfg.hh_hl_window)
    # 长均线 + 最近摆动低点 (结构破坏止损的判定基准)
    out["ma_long"] = c.rolling(120, min_periods=120).mean()
    out["ma_long_slope"] = (out["ma_long"] - out["ma_long"].shift(20)) / 20.0 / (out["ma_long"] + 1e-12)
    out["swing_low"] = structure.last_swing_extreme(l, 5, False, 5)
    out["swing_high"] = structure.last_swing_extreme(h, 5, True, 5)

    # ---- K线事件 (日K轻量版) ----
    out["body_ratio"] = candle.body_ratio(o, h, l, c)
    out["expansion"] = candle.expansion_ratio(h, l, c, out["atr"])
    out["bull_event"] = candle.bullish_event(
        o, h, l, c, out["atr"], cfg.body_min, cfg.expansion_min)
    out["bear_event"] = candle.bearish_event(
        o, h, l, c, out["atr"], cfg.body_min, cfg.expansion_min)
    out["false_bd"] = candle.false_breakdown(l, c, lo)
    out["false_bo"] = candle.false_breakout(h, c, up)

    # ---- 大结构判定 (强势结构 Gate): 区分"强势股整理"与"弱势股躺平" ----
    out["structure_state"] = structural_context.structural_context(
        c, h, l, df["vol"], ma_long=120)
    out["structural_ok"] = structural_context.is_structural_ok(out["structure_state"])

    # ---- 早期蓄势识别 (深跌反弹+缩量消化+结构未破的启动候选) ----
    acc = accumulation.accumulation_score(c, h, l, df["vol"])
    for col in ("acc_score", "acc_rebound", "acc_shrink", "acc_narrow", "acc_hold", "accum_state"):
        out[col] = acc[col]
    out["accumulating"] = ((out["accum_state"] == "ACCUMULATING")
                           | (out["accum_state"] == "REVERSAL_BOUNCE"))

    return out


def compute_trend_features(df: pd.DataFrame,
                           config: TrendConfig | None = None,
                           boundary_primary: int = 30) -> pd.DataFrame:
    """
    趋势系统特征宽表 (趋势指南第三~六章聚合入口)。

    输入 df 需含 open/high/low/close/vol (前复权), 按 trade_date 升序。
    与 compute_daily_features 共享特征层 (ER 与 DI 同源, 第三章), 输出趋势视角:
        er_{w} / er_slope / net_slope                     -- 方向效率回归
        vol_ratio / vol_pct / udvol / vol_persist / pulse_share
        obv / obv_slope / pv_corr / obv_confirm / bear_div / cmf / vwap / vwap_slope / vwap_dev_pct
        swing_high / swing_low / pullback_depth / pullback_vol / integrity / hl_break
        up_{w} / lo_{w} / range_width / range_age         -- 突破边界
        breakout_vol_ok / wick / retest / false_break_flags
    """
    cfg = config or DEFAULT_TREND_CONFIG
    out = pd.DataFrame(index=df.index)

    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    v = df["vol"]

    # ---- 方向效率回归: 多尺度 ER (= 1 - DI, 同源复用) ----
    er_main = None
    for i, w in enumerate(cfg.er_windows):
        er_w = directional.efficiency_ratio(c, w)
        out[f"er_{w}"] = er_w
        if i == 1:
            er_main = er_w                       # er_20 为主尺度
    out["er_slope"] = er_main - er_main.shift(cfg.er_slope_k)
    out["er_peak"] = er_main.rolling(cfg.divergence_lookback,
                                     min_periods=1).max()   # 用于 ER 回落检测
    out["net_slope"] = structure.net_slope(c, cfg.er_windows[-1])

    # ---- 波动 / ATR (退出与回调度量用) ----
    out["atr"] = volatility.atr(h, l, c, 14)
    out["natr"] = volatility.natr(h, l, c, 14)

    # ---- 趋势边界 (突破检测的参照): quantile 边界 + 区间年龄 ----
    for w in (20, boundary_primary, 60):
        up_w, lo_w = boundary.quantile_boundary(c, w, 0.90, 0.10)
        out[f"up_{w}"] = up_w
        out[f"lo_{w}"] = lo_w
    up, lo = out[f"up_{boundary_primary}"], out[f"lo_{boundary_primary}"]
    out["range_width"] = boundary.range_width(up, lo)
    # 区间年龄: 价格持续处于当前 [lo, up] 内的连续天数 (假突破过滤用)
    inside = ((c >= lo) & (c <= up)).astype(int)
    out["range_age"] = inside.groupby((inside == 0).cumsum()).cumsum()

    # ---- 量能体系 (第五章, 硬门槛) ----
    out["vol_ratio"] = volume.volume_ratio(v, cfg.vol_persist_slow)
    out["vol_pct"] = volume.volume_pct(v, cfg.vol_persist_slow, 120)
    out["udvol"] = volume.updown_vol_ratio(c, v, cfg.udvol_window)
    out["vol_persist"] = volume.vol_persistence(v, cfg.vol_persist_fast, cfg.vol_persist_slow)
    out["pulse_share"] = volume.vol_pulse_share(v, cfg.vol_persist_fast)
    obv_line = volume.obv(c, v)
    out["obv"] = obv_line
    out["obv_slope"] = volume.obv_slope(c, v, cfg.obv_confirm_window, cfg.er_slope_k)
    out["pv_corr"] = volume.price_volume_corr(c, obv_line, cfg.pv_corr_window)
    out["obv_confirm"] = volume.obv_confirm(c, obv_line, cfg.obv_confirm_window)
    out["bear_div"] = volume.bearish_divergence(c, obv_line, cfg.divergence_lookback)
    out["cmf"] = volume.cmf(h, l, c, v, cfg.cmf_window)
    vwap = volume.rolling_vwap(h, l, c, v, cfg.vwap_window)
    out["vwap"] = vwap
    out["vwap_slope"] = volume.vwap_slope(h, l, c, v, cfg.vwap_window, cfg.vwap_slope_k)
    out["vwap_dev_pct"] = (c - vwap) / (vwap + directional.EPS)   # 价格相对 VWAP 偏离%

    # ---- 趋势结构 (第四章) ----
    out["swing_high"] = structure.last_swing_extreme(
        h, cfg.swing_window, True, cfg.swing_confirm_lag)
    out["swing_low"] = structure.last_swing_extreme(
        l, cfg.swing_window, False, cfg.swing_confirm_lag)
    out["pullback_depth"] = structure.pullback_depth(h, l, c, cfg.swing_window,
                                                     cfg.swing_confirm_lag)
    out["integrity"] = structure.structure_integrity(h, l, cfg.integrity_window)
    out["hl_break"] = structure.hl_break(l, c, cfg.swing_window, cfg.swing_confirm_lag)
    # 回调量能比: 近5日均量 / 前段上涨均量
    out["pullback_vol"] = out["vol_persist"]

    # ---- 突破质量 (第六章) ----
    out["breakout_vol_ok"] = breakout.breakout_volume_ok(out["vol_ratio"],
                                                         cfg.breakout_vol_ratio)
    out["wick_ratio"] = breakout.upper_wick_ratio(o, h, l, c)
    out["escape"] = breakout.breakout_escape(c, up)
    out["retest"] = breakout.breakout_retest(c, v, up, horizon=5)
    out["false_break"] = breakout.false_breakout_flags(
        c, out["vol_ratio"], up, o, h, l, out["range_width"], out["range_age"],
        vol_min=cfg.breakout_vol_min)

    # ---- 长期均线 (Stage 检测) ----
    out["ma_long"] = c.rolling(cfg.stage_ma_long, min_periods=cfg.stage_ma_long).mean()
    out["above_ma"] = c > out["ma_long"]

    return out
