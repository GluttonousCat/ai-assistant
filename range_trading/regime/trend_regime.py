# -*- encoding: utf-8 -*-
"""
趋势 Regime 分析入口: 特征 -> TQS 打分 -> 状态机 -> 入场事件 -> TrendState

对偶 regime/daily_regime.py。run_trend_regime(df) 完成单标的全流程,
返回逐日明细与最后一日的 TrendState 快照 (趋势 Scanner Ranking 数据源)。
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pandas as pd

from range_trading.config import TrendConfig, DEFAULT_TREND_CONFIG
from range_trading.features import compute_trend_features
from range_trading.regime.trend_state_machine import (
    TrendState,
    TrendStateMachine,
    build_trend_state,
)
from range_trading.scoring.trend_score import score_trend


def _chandelier_stop(high: pd.Series, atr: pd.Series,
                     window: int, mult: float) -> pd.Series:
    """Chandelier 移动止损 (趋势指南 11.2): max(High_window) - mult*ATR, 只上移不下调"""
    raw = high.rolling(window, min_periods=window).max() - mult * atr
    return raw.cummax()


def run_trend_regime(df: pd.DataFrame, symbol: str = "",
                     config: TrendConfig | None = None) -> Tuple[pd.DataFrame, Optional[TrendState]]:
    """
    趋势全流程分析。

    参数:
        df: 按 trade_date 升序, 含 open/high/low/close/vol (前复权)
        symbol: 标的代码
    返回:
        (明细 DataFrame, 最后交易日 TrendState; 数据不足时 state 为 None)
    """
    cfg = config or DEFAULT_TREND_CONFIG
    feat = compute_trend_features(df, cfg)
    scored = score_trend(feat, cfg)

    merged = feat.join(scored)
    merged["trade_date"] = df["trade_date"].values
    merged["close"] = df["close"].values
    merged["high"] = df["high"].values
    # Chandelier 止损
    merged["chandelier_stop"] = _chandelier_stop(
        df["high"], merged["atr"], cfg.chandelier_window, cfg.chandelier_atr)

    sm = TrendStateMachine(cfg)
    states = sm.run(merged)
    merged["state"] = states

    last_valid = merged.dropna(subset=["tqs"])
    if last_valid.empty:
        return merged, None
    return merged, build_trend_state(symbol, last_valid.iloc[-1].to_dict(), cfg)
