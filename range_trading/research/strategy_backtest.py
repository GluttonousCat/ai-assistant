# -*- encoding: utf-8 -*-
"""
策略收益回测 (Strategy Backtest): 对比多种下注策略的历史收益

【目标】回答: 哪种信号 + 入场 + 出场组合的历史期望收益最高?

【回测口径】(事件驱动, 无前视, 指南第四十五章)
  - 信号日 t: 复用 run_daily_regime / run_trend_regime 得到当时快照;
  - 入场: t+1 开盘价 (t 时刻可见信息, t+1 可执行);
  - 出场: 触及 TP/SL 当日按该价平仓; 超过 horizon 未触及则最后一日收盘强平;
  - 收益: (出场价 - 入场价)/入场价, 含单笔手续费假设。

【震荡策略】(基于 RangeState: lower/upper/mid/width/range_pos/score/trend_risk)
  S1 基线:        全部区间族信号, TP=mid, SL=lower*0.98
  S2 高分:        score>=70
  S3 高分+下沿:    score>=70 且 range_pos<0.2        (用户直觉策略)
  S4 高+下沿+低风险: S3 + trend_risk==LOW
  S5 高+下沿+宽:   S3 + width>=0.10                  (赔率优先)

【趋势策略】
  S6 趋势回调:    TREND_ESTABLISHED + event==PULLBACK, TP=Chandelier 跟踪, SL=pullback 低点
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from core.logger import get_logger
from range_trading.config import DEFAULT_DAILY_CONFIG, DEFAULT_TREND_CONFIG
from range_trading.data.loader import load_daily_bars, get_latest_trade_date
from range_trading.regime.daily_regime import run_daily_regime
from range_trading.regime.trend_regime import run_trend_regime
from range_trading.regime.state_machine import RANGE_STATES
from storage.pg import PgClient

logger = get_logger("range_trading.backtest")

FEE = 0.0005          # 单边手续费 0.05%
HORIZON = 10          # 最大持有交易日
TP_SL_BUFFER = 0.98   # 止损缓冲 (下沿下方 2%)

OUT_DIR = Path("output/backtest")


# ================================================================ 交易模拟

def simulate_trade(df: pd.DataFrame, sig_idx: int, entry_rule: str,
                   tp_price: float, sl_price: float,
                   horizon: int = HORIZON) -> Optional[Dict[str, Any]]:
    """
    信号日 sig_idx, 次日开盘入场, 逐日判定 TP/SL, 返回交易结果。
    无前视: 只用 sig_idx 之前的信息定 TP/SL, 用 sig_idx+1 起的行情判定出场。
    """
    n = len(df)
    entry_idx = sig_idx + 1
    if entry_idx >= n:
        return None
    entry = df["open"].iloc[entry_idx]
    if not np.isfinite(entry) or entry <= 0:
        return None

    exit_price = None
    exit_idx = None
    exit_reason = "hold_close"
    for j in range(entry_idx, min(entry_idx + horizon, n)):
        h = df["high"].iloc[j]
        l = df["low"].iloc[j]
        # 止损优先 (保守: 同日触及 SL 和 TP 时按 SL 计)
        if l <= sl_price:
            exit_price = sl_price
            exit_idx = j
            exit_reason = "stop_loss"
            break
        if h >= tp_price:
            exit_price = tp_price
            exit_idx = j
            exit_reason = "take_profit"
            break
    if exit_price is None:
        exit_idx = min(entry_idx + horizon - 1, n - 1)
        exit_price = df["close"].iloc[exit_idx]

    gross = exit_price / entry - 1.0
    net = gross - 2 * FEE
    return {
        "entry": float(entry),
        "exit": float(exit_price),
        "exit_reason": exit_reason,
        "hold_days": int(exit_idx - entry_idx + 1),
        "gross_ret": float(gross),
        "net_ret": float(net),
        "win": bool(net > 0),
    }


# ================================================================ V2 改进版模拟

def _is_reversal_bar(df: pd.DataFrame, j: int, lower: float) -> bool:
    """
    反转确认K线 (下沿): 满足其一即确认
      A. 收阳 (Close > Open) 且收盘回到下沿上方;
      B. 长下影 (下影线 >= 实体2倍) 且收盘在下沿上方;
      C. 假跌破回收: Low 破下沿但 Close 收回下沿上方。
    """
    o = df["open"].iloc[j]
    h = df["high"].iloc[j]
    l = df["low"].iloc[j]
    c = df["close"].iloc[j]
    body = abs(c - o)
    lower_wick = min(o, c) - l
    cond_a = (c > o) and (c >= lower)
    cond_b = (lower_wick >= 2 * max(body, 1e-9)) and (c >= lower)
    cond_c = (l < lower) and (c >= lower)
    return bool(cond_a or cond_b or cond_c)


def simulate_range_v2(df: pd.DataFrame, sig_idx: int, lower: float, upper: float,
                      horizon: int = 20, wait_bars: int = 5) -> Optional[Dict[str, Any]]:
    """
    改进版震荡回测 (V2):
      入场: 信号日后 5 日内, 出现"下沿反转确认K线"当日收盘入场 (否则放弃);
      止盈: 上沿下方 2% (upper*0.98), 而非中部;
      止损: 区间失效 = 连续 2 日收盘在下沿下方 (lower*0.99 之下);
      到期: horizon 日未触及则收盘强平。
    """
    n = len(df)
    tp_price = upper * 0.98
    sl_level = lower * 0.99

    # ---- 入场: 等待反转确认 ----
    entry_idx = None
    for j in range(sig_idx + 1, min(sig_idx + 1 + wait_bars, n)):
        if _is_reversal_bar(df, j, lower):
            entry_idx = j
            break
    if entry_idx is None:
        return None
    entry = df["close"].iloc[entry_idx]
    if not np.isfinite(entry) or entry <= 0:
        return None

    # ---- 出场: 区间失效止损 / 上沿止盈 / 到期 ----
    below_close_run = 0
    exit_price = exit_idx = None
    exit_reason = "hold_close"
    for j in range(entry_idx, min(entry_idx + horizon, n)):
        h = df["high"].iloc[j]
        c = df["close"].iloc[j]
        # 止盈: 当日最高触及上沿下方
        if h >= tp_price:
            exit_price = tp_price
            exit_idx = j
            exit_reason = "take_profit"
            break
        # 区间失效: 连续 2 日收盘在下沿下方
        below_close_run = below_close_run + 1 if c < sl_level else 0
        if below_close_run >= 2:
            exit_price = c
            exit_idx = j
            exit_reason = "stop_loss"
            break
        exit_idx = j
    if exit_price is None:
        exit_idx = min(entry_idx + horizon - 1, n - 1)
        exit_price = df["close"].iloc[exit_idx]

    gross = exit_price / entry - 1.0
    net = gross - 2 * FEE
    return {
        "entry": float(entry),
        "exit": float(exit_price),
        "exit_reason": exit_reason,
        "hold_days": int(exit_idx - entry_idx + 1),
        "gross_ret": float(gross),
        "net_ret": float(net),
        "win": bool(net > 0),
    }



# ================================================================ 止损变体模拟器 (V3)
# 入场统一: t+1 开盘; 止盈统一: 区间中部 (与 G1 口径一致, 唯一变量是止损规则)。

def _finalize(df, entry, entry_idx, exit_idx, exit_price, exit_reason):
    gross = exit_price / entry - 1.0
    net = gross - 2 * FEE
    return {
        "entry": float(entry), "exit": float(exit_price), "exit_reason": exit_reason,
        "hold_days": int(exit_idx - entry_idx + 1),
        "gross_ret": float(gross), "net_ret": float(net), "win": bool(net > 0),
    }


def simulate_stop_variant(df: pd.DataFrame, sig_idx: int, rsig: Dict[str, Any],
                          stop_mode: str, horizon: int = 10,
                          vol_break: float = 1.5, reclaim_days: int = 2) -> Optional[Dict[str, Any]]:
    """
    止损变体统一模拟器。stop_mode:
      price            基线: 最低价碰 sl 即割 (当前口径)
      vol_confirm      P1 量价确认: 收盘破下沿 + 放量(>vol_break) + reclaim_days 内未收回
      structure        P2 结构破坏: 跌破 swing_low 或 (收破MA120 且 MA120 转降)
      false_break_hold P3 假跌破反向持有: 放量破下沿且不回收才割; 缩量破/快速收回 -> 持有
    """
    n = len(df)
    entry_idx = sig_idx + 1
    if entry_idx >= n:
        return None
    entry = df["open"].iloc[entry_idx]
    if not np.isfinite(entry) or entry <= 0:
        return None

    lower = rsig["lower"]
    tp = rsig["tp"]
    sl_price = rsig["sl"]
    vol_ma20 = rsig.get("vol_ma20") or 0.0
    swing_low = rsig.get("swing_low")
    ma120 = rsig.get("ma120")
    ma120_slope = rsig.get("ma120_slope")

    broke_day = -1
    for j in range(entry_idx, min(entry_idx + horizon, n)):
        h = df["high"].iloc[j]
        l = df["low"].iloc[j]
        c = df["close"].iloc[j]
        v = df["vol"].iloc[j]
        vol_ratio = v / vol_ma20 if vol_ma20 > 0 else 1.0

        # 止盈 (各变体一致, 优先)
        if h >= tp:
            return _finalize(df, entry, entry_idx, j, tp, "take_profit")

        if stop_mode == "price":
            if l <= sl_price:
                return _finalize(df, entry, entry_idx, j, sl_price, "stop_loss")

        elif stop_mode == "vol_confirm":
            if c < lower and vol_ratio >= vol_break and broke_day < 0:
                broke_day = j
            if broke_day >= 0 and c >= lower:
                broke_day = -1
            if broke_day >= 0 and (j - broke_day) >= reclaim_days:
                return _finalize(df, entry, entry_idx, j, c, "stop_loss")

        elif stop_mode == "structure":
            hl_broken = (swing_low is not None and np.isfinite(swing_low) and l < swing_low)
            ma_broken = (ma120 is not None and np.isfinite(ma120) and c < ma120
                         and ma120_slope is not None and np.isfinite(ma120_slope) and ma120_slope < 0)
            if hl_broken or ma_broken:
                return _finalize(df, entry, entry_idx, j, c, "stop_loss")

        elif stop_mode == "false_break_hold":
            if c < lower and vol_ratio >= vol_break:
                if broke_day < 0:
                    broke_day = j
                elif (j - broke_day) >= reclaim_days:
                    return _finalize(df, entry, entry_idx, j, c, "stop_loss")
            elif c >= lower and broke_day >= 0:
                broke_day = -1

    exit_idx = min(entry_idx + horizon - 1, n - 1)
    return _finalize(df, entry, entry_idx, exit_idx, df["close"].iloc[exit_idx], "hold_close")


# ================================================================ 入场过滤 + 分批止盈 (E系)

def simulate_entry_filter(df: pd.DataFrame, sig_idx: int, rsig: Dict[str, Any],
                          horizon: int = 10, wait_bars: int = 5,
                          shrink_vol: float = 0.9) -> Optional[Dict[str, Any]]:
    """
    E1 入场过滤 (源头降止损率): 信号日后 wait_bars 日内,
    只在出现"缩量止跌K线"时才入场 (收盘在下沿上方, 且量能 < 前一日量 × shrink_vol,
    且当日不收新低), 否则放弃该信号 (不强买)。
    出场沿用 P0 小止损 (碰下沿-2% 即割) + 中部止盈。
    """
    n = len(df)
    lower = rsig["lower"]
    tp = rsig["tp"]
    sl = rsig["sl"]

    entry_idx = None
    for j in range(sig_idx + 1, min(sig_idx + 1 + wait_bars, n)):
        o, h, l, c = (df[k].iloc[j] for k in ("open", "high", "low", "close"))
        v_prev = df["vol"].iloc[j - 1]
        v = df["vol"].iloc[j]
        low_prev = df["low"].iloc[j - 1]
        # 缩量止跌: 收在下沿上方 + 缩量 + 不收新低
        if c >= lower and v < v_prev * shrink_vol and l >= low_prev:
            entry_idx = j
            break
    if entry_idx is None:
        return None
    entry = df["open"].iloc[min(entry_idx + 1, n - 1)]
    if not np.isfinite(entry) or entry <= 0:
        return None

    for j in range(entry_idx + 1, min(entry_idx + 1 + horizon, n)):
        h = df["high"].iloc[j]
        l = df["low"].iloc[j]
        if l <= sl:
            return _finalize(df, entry, entry_idx, j, sl, "stop_loss")
        if h >= tp:
            return _finalize(df, entry, entry_idx, j, tp, "take_profit")
    exit_idx = min(entry_idx + horizon, n - 1)
    return _finalize(df, entry, entry_idx, exit_idx, df["close"].iloc[exit_idx], "hold_close")


def simulate_partial_tp(df: pd.DataFrame, sig_idx: int, rsig: Dict[str, Any],
                        horizon: int = 10, first_frac: float = 0.5) -> Optional[Dict[str, Any]]:
    """
    E2 分批止盈: 价格回到"区间下沿上方 + (上沿-下沿)*25%"先出 first_frac 锁定,
    剩余仓位博区间中部; 止损仍碰下沿-2% 即割 (全部仓位)。
    收益 = first_frac×(第一档收益) + (1-first_frac)×(剩余档收益)。
    """
    n = len(df)
    entry_idx = sig_idx + 1
    if entry_idx >= n:
        return None
    entry = df["open"].iloc[entry_idx]
    if not np.isfinite(entry) or entry <= 0:
        return None

    lower = rsig["lower"]
    upper = rsig["upper"]
    tp = rsig["tp"]
    sl = rsig["sl"]
    # 第一档目标: 下沿向上 25% 区间高度 (锁定反弹的一半)
    tp1 = lower + (upper - lower) * 0.25

    leg1_done = False
    leg1_ret = 0.0
    for j in range(entry_idx, min(entry_idx + horizon, n)):
        h = df["high"].iloc[j]
        l = df["low"].iloc[j]
        c = df["close"].iloc[j]
        # 止损: 全部仓位出场
        if l <= sl:
            ret = (sl / entry - 1.0) if not leg1_done else (
                first_frac * leg1_ret + (1 - first_frac) * (sl / entry - 1.0))
            net = ret - 2 * FEE
            return {"entry": float(entry), "exit": float(sl), "exit_reason": "stop_loss",
                    "hold_days": int(j - entry_idx + 1), "gross_ret": float(ret),
                    "net_ret": float(net), "win": bool(net > 0)}
        # 第一档止盈
        if not leg1_done and h >= tp1:
            leg1_done = True
            leg1_ret = tp1 / entry - 1.0
        # 第二档止盈 (中部)
        if leg1_done and h >= tp:
            ret = first_frac * leg1_ret + (1 - first_frac) * (tp / entry - 1.0)
            net = ret - 2 * FEE
            return {"entry": float(entry), "exit": float(tp), "exit_reason": "take_profit",
                    "hold_days": int(j - entry_idx + 1), "gross_ret": float(ret),
                    "net_ret": float(net), "win": bool(net > 0)}
    # 到期强平
    exit_idx = min(entry_idx + horizon - 1, n - 1)
    c = df["close"].iloc[exit_idx]
    ret = (c / entry - 1.0) if not leg1_done else (
        first_frac * leg1_ret + (1 - first_frac) * (c / entry - 1.0))
    net = ret - 2 * FEE
    return {"entry": float(entry), "exit": float(c), "exit_reason": "hold_close",
            "hold_days": int(exit_idx - entry_idx + 1), "gross_ret": float(ret),
            "net_ret": float(net), "win": bool(net > 0)}


# ================================================================ 信号采集

def _range_signals(df: pd.DataFrame, sig_idx: int) -> Optional[Dict[str, Any]]:
    """震荡信号: 在 sig_idx 日跑 regime, 返回入场参数"""
    hist = df.iloc[: sig_idx + 1]
    if len(hist) < 200:
        return None
    detail, state = run_daily_regime(hist, symbol="")
    if state is None or state.state not in RANGE_STATES:
        return None
    if np.isnan(state.upper) or np.isnan(state.lower) or state.upper <= state.lower:
        return None
    mid = (state.upper + state.lower) / 2.0
    # 结构止损/量价止损所需的辅助量 (取自特征宽表最后一行, 无前视)
    last = detail.iloc[-1]
    vol_ma20 = float(df["vol"].iloc[max(0, sig_idx - 19): sig_idx + 1].mean())
    ma120 = float(last["ma_long"]) if "ma_long" in detail.columns and np.isfinite(last.get("ma_long", np.nan)) else np.nan
    ma120_slope = float(last["ma_long_slope"]) if "ma_long_slope" in detail.columns and np.isfinite(last.get("ma_long_slope", np.nan)) else np.nan
    swing_low = float(last["swing_low"]) if "swing_low" in detail.columns and np.isfinite(last.get("swing_low", np.nan)) else np.nan
    return {
        "state": state.state,
        "range_score": state.range_score,
        "range_pos": state.range_pos,
        "trend_risk": state.trend_risk,
        "width": state.width,
        "structure_state": getattr(state, "structure_state", "NEUTRAL"),
        "structural_ok": bool(getattr(state, "structural_ok", False)),
        "lower": state.lower,
        "upper": state.upper,
        "mid": mid,
        "vol_ma20": vol_ma20,
        "ma120": ma120,
        "ma120_slope": ma120_slope,
        "swing_low": swing_low,
        "tp": mid,                       # 止盈: 区间中部
        "sl": state.lower * TP_SL_BUFFER,  # 止损: 下沿下方 2%
    }


def _trend_signals(df: pd.DataFrame, sig_idx: int) -> Optional[Dict[str, Any]]:
    """趋势信号: TREND 族状态 + 有效入场事件 (BREAKOUT/PULLBACK/RETEST)"""
    hist = df.iloc[: sig_idx + 1]
    if len(hist) < 200:
        return None
    detail, state = run_trend_regime(hist, symbol="")
    if state is None or state.state not in ("TREND_FORMING", "TREND_ESTABLISHED"):
        return None
    if state.event not in ("PULLBACK", "BREAKOUT", "RETEST"):
        return None
    if np.isnan(state.chandelier_stop) or state.chandelier_stop <= 0:
        return None
    close = df["close"].iloc[sig_idx]
    return {
        "state": state.state,
        "tqs": state.tqs,
        "vpa": state.vpa,
        "event": state.event,
        "pullback_depth": state.pullback_depth,
        "close": close,
        "tp": close * 1.10,              # 趋势止盈: 暂用 +10% (Chandelier 跟踪需逐日, 简化)
        "sl": state.chandelier_stop,      # 止损: Chandelier
    }


# ================================================================ 策略规则

STRATEGIES = {
    "S1_基线_全信号": lambda s: True,
    "S2_高分70": lambda s: s["range_score"] >= 70,
    "S3_高分+下沿": lambda s: s["range_score"] >= 70 and s["range_pos"] < 0.2,
    "S4_高分+下沿+低风险": lambda s: (s["range_score"] >= 70 and s["range_pos"] < 0.2
                                       and s["trend_risk"] == "LOW"),
    "S5_高分+下沿+宽10": lambda s: (s["range_score"] >= 70 and s["range_pos"] < 0.2
                                      and s["width"] >= 0.10),
}

# V2 改进策略 (反转确认入场 + 上沿止盈 + 区间失效止损 + 20日持有):
#   V1 高分+下沿+反转确认       (核心改进: 等反转K线, 上沿止盈, 失效止损)
#   V2 高分+下沿+反转+低风险    (再加趋势风险过滤)
#   V3 高分+下沿+反转+宽10      (宽度保证利润空间)
STRATEGIES_V2 = {
    "V1_反转确认+上沿止盈": lambda s: s["range_score"] >= 70 and s["range_pos"] < 0.2,
    "V2_反转+下沿+低风险": lambda s: (s["range_score"] >= 70 and s["range_pos"] < 0.2
                                          and s["trend_risk"] == "LOW"),
    "V3_反转+下沿+宽10": lambda s: (s["range_score"] >= 70 and s["range_pos"] < 0.2
                                        and s["width"] >= 0.10),
}

# V3 结构 Gate 策略 (核心逻辑: 先找强势结构, 再在强势结构里等整理买点):
#   用 V1 口径 (中部止盈/下沿止损) 以便与 S 系对照, 唯一变量是 structure Gate。
#   G1 优质结构+下沿      : structural_ok + 下沿 (任意分数)
#   G2 优质结构+高分+下沿  : structural_ok + score>=70 + 下沿  (最强过滤)
#   G3 弱结构+高分+下沿    : 非优质结构 + score>=70 + 下沿  (对照组, 验证 Gate 价值)
#   G4 优质结构+震荡上行   : 生益沪电式 (UP_CHANNEL) + 下沿
STRATEGIES_V3 = {
    "G1_优质结构+下沿": lambda s: s.get("structural_ok") and s["range_pos"] < 0.2,
    "G2_优质结构+高分+下沿": lambda s: (s.get("structural_ok") and s["range_score"] >= 70
                                          and s["range_pos"] < 0.2),
    "G3_弱结构+高分+下沿": lambda s: (not s.get("structural_ok") and s["range_score"] >= 70
                                         and s["range_pos"] < 0.2),
    "G4_震荡上行+下沿": lambda s: (s.get("structure_state") == "UP_CHANNEL"
                                     and s["range_pos"] < 0.2),
}

# 止损变体对比 (统一 G1 入场: 优质结构+下沿; 唯一变量是止损规则):
#   P0 价格止损 (基线, 碰下沿-2%即割)
#   P1 量价确认止损 (收盘破+放量+未回收)
#   P2 结构破坏止损 (破HL / 破MA120且转降)
#   P3 假跌破反向持有 (缩量破+放量收回不割)
STOP_ENTRY = lambda s: s.get("structural_ok") and s["range_pos"] < 0.2   # noqa: E731
STRATEGIES_STOP = {
    "P0_价格止损(基线)": "price",
    "P1_量价确认止损": "vol_confirm",
    "P2_结构破坏止损": "structure",
    "P3_假跌破反向持有": "false_break_hold",
}

# E系 入场过滤 + 分批止盈 (统一优质结构+下沿的土壤, 改进入场确认与出场结构):
#   E1 入场过滤: 缩量止跌确认才入场 (源头过滤会被误割的弱单), 出场=P0小止损+中部止盈
#   E2 分批止盈: 下沿上方25%先出一半锁定, 剩余博中部, 止损=P0
STRATEGIES_ENTRY = {
    "E1_缩量止跌入场": "entry_filter",
    "E2_分批止盈": "partial_tp",
}


# ================================================================ 主回测

def run_backtest(anchor_dates: List[date], min_amount: float = 10000.0,
                 symbol_limit: Optional[int] = None) -> pd.DataFrame:
    """对每个锚点日, 全 universe 生成信号并模拟交易, 返回交易明细"""
    all_trades: List[Dict[str, Any]] = []
    for ad in sorted(anchor_dates):
        all_trades.extend(run_backtest_single_anchor(ad, min_amount, symbol_limit))
    return pd.DataFrame(all_trades)


def run_backtest_single_anchor(ad: date, min_amount: float = 10000.0,
                               symbol_limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """单锚点回测 (每批独立连接), 返回该锚点的交易明细列表"""
    trades: List[Dict[str, Any]] = []

    with PgClient() as pg:
        universe = _active_universe(pg, ad, min_amount)
    if symbol_limit:
        universe = universe[:symbol_limit]
    # 拉取窗口需覆盖 2019-03~2024-01 数据空洞: 用 800 天保证 2024-01-25 后
    # 有 >=200 个交易日预热 (NATR分位250D / 边界60D 等长窗口指标需要)
    start = ad - timedelta(days=800)
    end = ad + timedelta(days=HORIZON * 2 + 40)   # V2 持有20日+等待5日, 加大缓冲
    logger.info(f"{ad} universe {len(universe)} 只")

    for ts_code, df in _iter_frames_fresh_conn(universe, start, end):
        df = df.sort_values("trade_date").reset_index(drop=True)
        dates = df["trade_date"].dt.date.values
        idx = int(np.searchsorted(dates, ad))
        if idx >= len(df) or dates[idx] != ad:
            continue

        # ---- 震荡策略 ----
        try:
            rsig = _range_signals(df, idx)
        except Exception:
            rsig = None
        if rsig:
            tr = simulate_trade(df, idx, "range", rsig["tp"], rsig["sl"])
            if tr:
                for sname, rule in STRATEGIES.items():
                    if rule(rsig):
                        trades.append({"date": ad, "symbol": ts_code,
                                       "strategy": sname, "family": "range",
                                       **{k: rsig[k] for k in ("range_score", "range_pos", "width")},
                                       **tr})
            # ---- V2 改进策略: 下沿反转确认入场 + 上沿止盈 + 区间失效止损 ----
            for vname, vrule in STRATEGIES_V2.items():
                if vrule(rsig):
                    tr2 = simulate_range_v2(df, idx, rsig["lower"], rsig["upper"])
                    if tr2:
                        trades.append({"date": ad, "symbol": ts_code,
                                       "strategy": vname, "family": "range_v2",
                                       **{k: rsig[k] for k in ("range_score", "range_pos", "width")},
                                       **tr2})
            # ---- V3 结构 Gate 策略 (V1 口径, 唯一变量是 structure) ----
            for gname, grule in STRATEGIES_V3.items():
                if grule(rsig):
                    tr3 = simulate_trade(df, idx, "range", rsig["tp"], rsig["sl"])
                    if tr3:
                        trades.append({"date": ad, "symbol": ts_code,
                                       "strategy": gname, "family": "range_v3",
                                       "structure_state": rsig.get("structure_state", "NEUTRAL"),
                                       **{k: rsig[k] for k in ("range_score", "range_pos", "width")},
                                       **tr3})
            # ---- 止损变体对比 (统一 G1 入场, 唯一变量是止损规则) ----
            if STOP_ENTRY(rsig):
                for pname, pmode in STRATEGIES_STOP.items():
                    trp = simulate_stop_variant(df, idx, rsig, pmode)
                    if trp:
                        trades.append({"date": ad, "symbol": ts_code,
                                       "strategy": pname, "family": "range_stop",
                                       "structure_state": rsig.get("structure_state", "NEUTRAL"),
                                       **{k: rsig[k] for k in ("range_score", "range_pos", "width")},
                                       **trp})
                # ---- E系: 入场过滤 + 分批止盈 ----
                tre = simulate_entry_filter(df, idx, rsig)
                if tre:
                    trades.append({"date": ad, "symbol": ts_code,
                                   "strategy": "E1_缩量止跌入场", "family": "range_entry",
                                   "structure_state": rsig.get("structure_state", "NEUTRAL"),
                                   **{k: rsig[k] for k in ("range_score", "range_pos", "width")},
                                   **tre})
                trp2 = simulate_partial_tp(df, idx, rsig)
                if trp2:
                    trades.append({"date": ad, "symbol": ts_code,
                                   "strategy": "E2_分批止盈", "family": "range_entry",
                                   "structure_state": rsig.get("structure_state", "NEUTRAL"),
                                   **{k: rsig[k] for k in ("range_score", "range_pos", "width")},
                                   **trp2})
        # ---- 趋势策略 ----
        try:
            tsig = _trend_signals(df, idx)
        except Exception:
            tsig = None
        if tsig:
            tr = simulate_trade(df, idx, "trend", tsig["tp"], tsig["sl"])
            if tr:
                trades.append({"date": ad, "symbol": ts_code,
                               "strategy": "S6_趋势回调", "family": "trend",
                               "range_score": np.nan, "range_pos": np.nan,
                               "width": np.nan, **tr})
    return trades


def _active_universe(pg, as_of: date, min_amount: float) -> List[str]:
    window_start = as_of - timedelta(days=120)
    rows = pg.fetch_all(
        """SELECT ts_code FROM stock.daily WHERE trade_date BETWEEN %s AND %s
           GROUP BY ts_code HAVING COUNT(*) >= 60 AND AVG(amount) >= %s ORDER BY ts_code""",
        (window_start, as_of, min_amount))
    return [r["ts_code"] for r in rows]


def _iter_frames_fresh_conn(codes, start, end, batch_size=200):
    """
    逐批拉取, 每批用独立 PG 连接 (用完即还池)。
    解决长回测中共享连接因单批处理耗时、空闲被远端断开的问题:
    拉取 -> 立即关连接 -> 在内存里逐只 yield -> 下一批再开新连接。
    """
    from range_trading.data.loader import load_daily_bars
    codes = list(codes)
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        try:
            with PgClient() as pg:
                df = load_daily_bars(pg, batch, start, end)
        except Exception as e:
            logger.warning(f"批次 {i // batch_size} 拉取失败, 跳过: {e}")
            continue
        if df.empty:
            continue
        for ts_code, g in df.groupby("ts_code", sort=False):
            yield ts_code, g.reset_index(drop=True)


# ================================================================ 统计

def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    """按策略汇总: 胜率/盈亏比/期望收益/最大回撤"""
    trades = trades.copy()
    # date 列可能混有 str (CSV 读回) 与 date 对象, 统一转字符串便于排序
    trades["date"] = trades["date"].astype(str)
    rows = []
    for sname, g in trades.groupby("strategy"):
        n = len(g)
        wins = g[g["win"]]
        losses = g[~g["win"]]
        avg_win = wins["net_ret"].mean() if len(wins) else 0.0
        avg_loss = abs(losses["net_ret"].mean()) if len(losses) else 0.0
        profit_ratio = (avg_win / avg_loss) if avg_loss > 0 else np.nan
        win_rate = g["win"].mean()
        expectancy = g["net_ret"].mean()
        # 最大回撤: 按日期排序的累计净值最大回撤
        g_sorted = g.sort_values("date")
        cum = (1 + g_sorted["net_ret"]).cumprod()
        max_dd = float((cum / cum.cummax() - 1.0).min()) if len(cum) else 0.0
        rows.append({
            "strategy": sname, "n": n,
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_ratio": round(profit_ratio, 3) if np.isfinite(profit_ratio) else None,
            "expectancy": round(expectancy, 4),
            "expectancy_ann": round(expectancy * (250 / HORIZON), 3),  # 粗略年化
            "max_drawdown": round(max_dd, 4),
            "avg_hold_days": round(g["hold_days"].mean(), 1),
            "tp_rate": round((g["exit_reason"] == "take_profit").mean(), 4),
            "sl_rate": round((g["exit_reason"] == "stop_loss").mean(), 4),
        })
    return pd.DataFrame(rows).sort_values("expectancy", ascending=False).reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description="策略收益回测")
    parser.add_argument("--limit", type=int, default=None, help="限制 universe 数量 (调试)")
    parser.add_argument("--anchors", type=int, default=8, help="锚点数量")
    parser.add_argument("--resume", action="store_true", help="断点续跑: 跳过 trades.csv 已有锚点")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    anchor_dates = [
        date(2024, 12, 13),
        date(2025, 3, 14), date(2025, 6, 13), date(2025, 9, 12),
        date(2025, 12, 11),
        date(2026, 3, 13), date(2026, 6, 12),
        date(2026, 8, 14),
    ][: args.anchors]

    # 断点续跑: 每个锚点独立 CSV, 已完成则跳过, 中断后重跑只补剩余
    part_files = {ad: OUT_DIR / f"trades_{ad.strftime('%Y%m%d')}.csv" for ad in anchor_dates}
    frames = []
    for ad in anchor_dates:
        pf = part_files[ad]
        if args.resume and pf.exists():
            logger.info(f"{ad} 已完成, 跳过 (断点续跑)")
            frames.append(pd.read_csv(pf))
            continue
        t = run_backtest_single_anchor(ad, symbol_limit=args.limit)
        g = pd.DataFrame(t)
        if not g.empty:
            g.to_csv(pf, index=False, encoding="utf-8-sig")
            frames.append(g)
        logger.info(f"{ad} 完成, 交易 {len(g)} 笔, 已增量写盘")

    if not frames:
        logger.error("无交易产生")
        return
    trades = pd.concat(frames, ignore_index=True)
    trades.to_csv(OUT_DIR / "trades.csv", index=False, encoding="utf-8-sig")

    summary = summarize(trades)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print("\n========== 策略收益对比 ==========")
    print(summary.to_string(index=False))
    logger.info(f"明细: {OUT_DIR/'trades.csv'}, 汇总: {OUT_DIR/'summary.csv'}")


if __name__ == "__main__":
    main()
