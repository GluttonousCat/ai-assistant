"""
range_trading 历史校验 (Event Study)

回答工程指南的核心命题: "Range 信号能否提前识别正在形成的可交易震荡区间?"
分两个互补视角:

1. 前瞻性 (Signal-anchored): 信号日 t, 观察未来 horizon 日区间是否维持
   质量指标用"区间策略可直接兑现的口径", 而非方向收益:
   - persist_ratio  : 未来 N 日收盘落在信号日区间 [Lower, Upper] 内的比例
   - drift_natr     : 净漂移 |C_{t+N}-C_t| / ATR (区间应趋零)
   - range_holding  : persist>=persist_min 且 drift <= drift_max (二值标签)
   - no_breakout    : 未来 N 日无连续 breakout_confirm 天有效越界

2. 领先性 (Event-anchored): 事后标定真区间起点 anchor, 回看信号提前量
   真区间起点的事后定义 (仅用 t 及之前数据, 保证可复现):
   - di_main 持续高; di_slope 刚转正; 区间宽度适中; 价格落于区间内
   lead_time = anchor_idx - first_signal_idx (正数 = 信号早于区间形成)

【边界约束】所有未来量仅用于"评估标签", 信号生成严格 t 时点可见。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from range_trading.config import DailyConfig, DEFAULT_DAILY_CONFIG
from range_trading.features.directional import directional_inefficiency
from range_trading.regime.state_machine import RANGE_STATES

# 信号定义: 由 RangeState 快照判定
EARLY_STATES = {"RANGE_FORMATION", "EARLY_TRADABLE_RANGE", "MATURE_RANGE", "RANGE_RENEWAL"}


def is_range_signal(state: str, score: float, min_score: float) -> bool:
    return state in EARLY_STATES and score >= min_score


# ---------------------------------------------------------------- 前瞻性

def forward_metrics(df: pd.DataFrame, detail: pd.DataFrame, idx: int,
                    horizon: int, cfg: DailyConfig,
                    persist_min: float = 0.55, drift_max_atr: float = 3.0) -> Optional[dict]:
    """信号日 idx, 计算未来 horizon 日的区间维持度指标 (无足够未来数据返回 None)"""
    n = len(df)
    if idx + horizon >= n:
        return None
    up = detail["up"].iloc[idx]
    lo = detail["lo"].iloc[idx]
    atr_v = detail["atr"].iloc[idx]
    close_t = df["close"].iloc[idx]
    if np.isnan(up) or np.isnan(lo) or up <= lo or atr_v <= 0 or np.isnan(atr_v):
        return None

    closes = df["close"].iloc[idx + 1: idx + horizon + 1].to_numpy(dtype=float)
    width = up - lo
    persist = float(np.mean((closes >= lo) & (closes <= up)))
    drift_atr = abs(closes[-1] - close_t) / atr_v
    drift_width = abs(closes[-1] - close_t) / width
    # 连续有效越界 -> 突破
    outside = (closes > up) | (closes < lo)
    max_run = 0
    run = 0
    for o in outside:
        run = run + 1 if o else 0
        max_run = max(max_run, run)
    no_breakout = max_run < cfg.breakout_confirm_days

    return {
        "persist_ratio": persist,
        "drift_natr": float(drift_atr),
        "drift_width": float(drift_width),
        "no_breakout": bool(no_breakout),
        "range_holding": bool(persist >= persist_min and drift_atr <= drift_max_atr),
    }


# ---------------------------------------------------------------- 真区间起点标定

def find_range_anchors(df: pd.DataFrame, cfg: DailyConfig,
                       di_min: float = 0.60, di_persist: int = 3,
                       width_lo: float = 0.05, width_hi: float = 0.45,
                       min_gap: int = 30) -> list[int]:
    """
    事后标定"真区间起点"(仅用 t 及之前数据), 返回 anchor 行号列表。
    相邻 anchor 间隔 >= min_gap 去重。
    """
    close = df["close"]
    di = directional_inefficiency(close, cfg.di_windows[1])      # di_20
    di_slope = di - di.shift(cfg.di_slope_k)
    up = close.rolling(cfg.boundary_primary, min_periods=cfg.boundary_primary).quantile(cfg.upper_quantile)
    lo = close.rolling(cfg.boundary_primary, min_periods=cfg.boundary_primary).quantile(cfg.lower_quantile)
    mid = (up + lo) / 2.0
    width = (up - lo) / mid

    anchors: list[int] = []
    last = -10 ** 9
    for i in range(len(df)):
        if i < cfg.boundary_primary + cfg.di_windows[1]:
            continue
        if i - last < min_gap:
            continue
        di_v, slope_v, w_v = di.iloc[i], di_slope.iloc[i], width.iloc[i]
        c, u, l = close.iloc[i], up.iloc[i], lo.iloc[i]
        if np.isnan(di_v) or np.isnan(slope_v) or np.isnan(w_v) or np.isnan(u):
            continue
        # di 连续 di_persist 天维持高位
        di_ok = (di.iloc[i - di_persist + 1: i + 1] >= di_min).all()
        slope_ok = slope_v > 0.01                       # 方向效率刚开始失效
        width_ok = width_lo <= w_v <= width_hi
        inside = l <= c <= u
        if di_ok and slope_ok and width_ok and inside:
            anchors.append(i)
            last = i
    return anchors


def lead_time(detail: pd.DataFrame, anchor_idx: int, min_score: float,
              lookback: int = 10) -> tuple[int, bool]:
    """
    回看 anchor 前 lookback 天, 返回 (lead_days, detected):
    lead_days = anchor_idx - 首次信号行号; 未检测到返回 (0, False)。
    信号须在 anchor 之前或当天出现 (detected=True 且 lead>=0 视为领先/同步)。
    """
    start = max(0, anchor_idx - lookback)
    seg = detail.iloc[start: anchor_idx + 1]
    hit = None
    for j in range(len(seg)):
        row = seg.iloc[j]
        if is_range_signal(row["state"], row.get("daily_range_score", np.nan), min_score):
            hit = seg.index[j]
            break
    if hit is None:
        return 0, False
    # 位置差 (seg 为连续行)
    first_pos = start + list(seg.index).index(hit)
    return anchor_idx - first_pos, True
