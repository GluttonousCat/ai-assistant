"""
日K Regime 分析入口: 特征 -> 打分 -> 状态机 -> RangeState

run_daily_regime(df) 完成单标的全流程; 返回逐日明细 DataFrame
与最后一日的 RangeState 快照 (即 "日K系统最重要的输出", 指南第十七章)。
"""
from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd

from range_trading.config import DailyConfig, DEFAULT_DAILY_CONFIG
from range_trading.features import compute_daily_features
from range_trading.regime.state_machine import (
    DailyRangeStateMachine,
    RangeState,
    build_range_state,
)
from range_trading.scoring.daily_score import score_daily


def run_daily_regime(df: pd.DataFrame, symbol: str = "",
                     config: DailyConfig | None = None) -> Tuple[pd.DataFrame, Optional[RangeState]]:
    """
    日K全流程分析。

    参数:
        df: 按 trade_date 升序, 含 open/high/low/close (前复权) 与 vol/amount 列
        symbol: 标的代码 (仅用于 RangeState 标识)
    返回:
        (明细 DataFrame, 最后一个交易日的 RangeState; 数据不足时 state 为 None)
    """
    cfg = config or DEFAULT_DAILY_CONFIG
    feat = compute_daily_features(df, cfg)
    scored = score_daily(feat, cfg)

    merged = feat.join(scored)
    # 原始行情与主尺度别名列 (状态机 / RangeState 统一引用)
    merged["trade_date"] = df["trade_date"].values
    for col in ("close", "high", "low", "open"):
        merged[col] = df[col].values
    merged["up"] = merged[f"up_{cfg.boundary_primary}"]
    merged["lo"] = merged[f"lo_{cfg.boundary_primary}"]
    merged["di"] = merged[f"di_{cfg.di_windows[1]}"]
    merged["flip"] = merged[f"flip_{cfg.flip_windows[-1]}"]

    sm = DailyRangeStateMachine(cfg)
    states, ages = sm.run(merged)
    merged["state"] = states
    merged["age"] = ages

    last_valid = merged.dropna(subset=["daily_range_score"])
    if last_valid.empty:
        return merged, None
    return merged, build_range_state(symbol, last_valid.iloc[-1].to_dict())
