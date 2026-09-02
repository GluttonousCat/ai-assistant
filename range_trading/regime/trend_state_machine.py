# -*- encoding: utf-8 -*-
"""
趋势状态机 (趋势指南第九章): 独立于震荡 DailyRangeStateMachine

转移路径:
    无信号 (NONE)
      │ 突破三要素 (越界 + 量能 + 站稳) 且非假突破
      ▼
    BREAKOUT_CONFIRMED
      │ TQS >= 40 连续 confirm_days 且 VPA 达标
      ▼
    TREND_FORMING
      │ TQS >= 60 连续 confirm_days 且 Stage = 2
      ▼
    TREND_ESTABLISHED
      │ 衰竭证据组合 (背离 + ER回落 + HL破坏 任二)
      ▼
    TREND_EXHAUSTION ──(hold_days)──► NONE (交还震荡工作区)

降级: 任一趋势态 TQS 连续 fallback_days < forming 阈值 -> 回退。
与震荡系统共享 Regime 枚举, 但状态机独立运行 (两系统由 Regime Gate 协同)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from range_trading.config import TrendConfig, DEFAULT_TREND_CONFIG
from range_trading.regime.state_machine import Regime
from range_trading.scoring.trend_score import trend_stage_label


@dataclass
class TrendState:
    """趋势标的单日状态快照 (趋势指南第十二章 Ranking 数据源)"""
    symbol: str
    trade_date: Any
    state: str
    tqs: float
    vpa: float
    stage: int
    stage_label: str
    er20: float
    er_slope: float
    udvol: float
    cmf: float
    pullback_depth: float
    integrity: float
    vol_ratio: float
    close: float
    vwap_dev_pct: float
    event: str                 # 入场事件: NONE/BREAKOUT/PULLBACK/RETEST
    exhaust_evidence: float    # 衰竭证据强度 0~100
    bear_div: bool
    hl_break: bool
    pulse_share: float = 0.0   # 近5日最大单日量占比 (游资脉冲检测, >0.5 危险)
    pulse_penalty: float = 0.0 # 脉冲硬扣分 (已从 TQS 扣除)
    # 出场参考 (Chandelier)
    chandelier_stop: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            k: (v.item() if isinstance(v, np.generic) else v)
            for k, v in self.__dict__.items()
        }


class TrendStateMachine:
    """趋势状态机 (逐日推进)"""

    def __init__(self, config: TrendConfig | None = None):
        self.cfg = config or DEFAULT_TREND_CONFIG
        self.state: Optional[Regime] = None          # None = 无趋势信号
        self._streak_forming = 0
        self._streak_established = 0
        self._streak_weak = 0
        self._streak_exhaust = 0
        self._breakout_day = -1
        self._exhaust_start = -1

    def _update(self, row: Dict[str, Any]) -> None:
        cfg = self.cfg
        tqs = row.get("tqs", 0.0) or 0.0
        vpa = row.get("vpa", 0.0) or 0.0
        self._streak_forming = (
            self._streak_forming + 1
            if (tqs >= cfg.tqs_forming and vpa >= cfg.vpa_min_entry) else 0)
        self._streak_established = (
            self._streak_established + 1
            if (tqs >= cfg.tqs_established and vpa >= cfg.vpa_min_entry
                and row.get("stage") == 2) else 0)
        self._streak_weak = self._streak_weak + 1 if tqs < cfg.tqs_forming else 0
        # 衰竭: 背离/ER回落/HL破坏 任二
        ev = int(bool(row.get("bear_div"))) + int(bool(row.get("er_fall", 0) > 0.5)) \
            + int(bool(row.get("hl_break")))
        self._streak_exhaust = self._streak_exhaust + 1 if ev >= 2 else 0

    def step(self, i: int, row: Dict[str, Any]) -> Optional[Regime]:
        cfg = self.cfg
        self._update(row)
        s = self.state

        breakout_ok = (bool(row.get("escape")) and bool(row.get("breakout_vol_ok"))
                       and not bool(row.get("false_break")))

        if s is None:
            if breakout_ok:
                self.state = Regime.BREAKOUT_CONFIRMED
                self._breakout_day = i

        elif s == Regime.BREAKOUT_CONFIRMED:
            if self._streak_forming >= cfg.trend_confirm_days:
                self.state = Regime.TREND_FORMING
            elif self._streak_weak >= cfg.trend_fallback_days:
                self.state = None                            # 突破失败, 回到无信号

        elif s == Regime.TREND_FORMING:
            if self._streak_established >= cfg.trend_confirm_days:
                self.state = Regime.TREND_ESTABLISHED
            elif self._streak_exhaust >= 1:
                self.state = Regime.TREND_EXHAUSTION
                self._exhaust_start = i
            elif self._streak_weak >= cfg.trend_fallback_days:
                self.state = None

        elif s == Regime.TREND_ESTABLISHED:
            if self._streak_exhaust >= 1:
                self.state = Regime.TREND_EXHAUSTION
                self._exhaust_start = i
            elif self._streak_weak >= cfg.trend_fallback_days:
                self.state = Regime.TREND_FORMING            # 降级

        elif s == Regime.TREND_EXHAUSTION:
            if i - self._exhaust_start >= cfg.exhaustion_hold_days:
                self.state = None                            # 交还震荡工作区

        return self.state

    def run(self, df: pd.DataFrame) -> List[Optional[str]]:
        states: List[Optional[str]] = []
        rows = df.to_dict("records")
        for i, rec in enumerate(rows):
            st = self.step(i, rec)
            states.append(st.value if st else "NONE")
        return states


def detect_event(row: Dict[str, Any], state: Optional[str],
                 cfg: TrendConfig) -> str:
    """
    入场事件检测 (趋势指南第十章):
      PULLBACK  趋势确立 + 缩量回调 + 回踩 VWAP 企稳  (核心, 标准仓)
      RETEST    突破后回踩原上沿不破 + 缩量
      BREAKOUT  放量有效突破 (试探仓)
    """
    if state not in (Regime.TREND_FORMING.value, Regime.TREND_ESTABLISHED.value,
                     Regime.BREAKOUT_CONFIRMED.value):
        return "NONE"
    vpa = row.get("vpa", 0.0) or 0.0
    depth = row.get("pullback_depth")
    pvol = row.get("pullback_vol", 1.0) or 1.0
    vdev = row.get("vwap_dev_pct")

    # Pullback: 回调深度在入场区 + 缩量 + 价格回踩 VWAP 附近
    if (state in (Regime.TREND_FORMING.value, Regime.TREND_ESTABLISHED.value)
            and depth is not None and not np.isnan(depth)
            and cfg.pullback_entry_zone[0] <= depth <= cfg.pullback_entry_zone[1]
            and pvol <= cfg.pullback_vol_max
            and vdev is not None and not np.isnan(vdev) and abs(vdev) < 0.02):
        return "PULLBACK"
    # Retest
    if bool(row.get("retest")) and vpa >= cfg.vpa_min_entry:
        return "RETEST"
    # Breakout
    if (bool(row.get("escape")) and bool(row.get("breakout_vol_ok"))
            and vpa >= cfg.vpa_min_entry):
        return "BREAKOUT"
    return "NONE"


def build_trend_state(symbol: str, rec: Dict[str, Any],
                      cfg: TrendConfig) -> TrendState:
    def _f(key, default=np.nan):
        v = rec.get(key, default)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return float(v)
    stage = int(rec.get("stage", 1) or 1)
    state = rec.get("state", "NONE")
    event = detect_event(rec, state, cfg)
    # Chandelier 止损 (指南 11.2): 需 high 窗口, 这里用 close+ATR 近似由调用方补
    return TrendState(
        symbol=symbol,
        trade_date=rec.get("trade_date"),
        state=state,
        tqs=_f("tqs", 0.0),
        vpa=_f("vpa", 0.0),
        stage=stage,
        stage_label=trend_stage_label(stage),
        er20=_f("er_20"),
        er_slope=_f("er_slope"),
        udvol=_f("udvol"),
        cmf=_f("cmf"),
        pullback_depth=_f("pullback_depth"),
        integrity=_f("integrity"),
        vol_ratio=_f("vol_ratio"),
        close=_f("close"),
        vwap_dev_pct=_f("vwap_dev_pct"),
        event=event,
        exhaust_evidence=_f("exhaust_evidence", 0.0),
        bear_div=bool(rec.get("bear_div", False)),
        hl_break=bool(rec.get("hl_break", False)),
        pulse_share=_f("pulse_share", 0.0),
        pulse_penalty=_f("pulse_penalty", 0.0),
        chandelier_stop=_f("chandelier_stop"),
    )
