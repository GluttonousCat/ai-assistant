"""
日K层状态机: TREND -> TREND_DECAY -> RANGE_FORMATION -> EARLY_TRADABLE_RANGE
            -> MATURE_RANGE -> BREAKOUT / BREAKOUT_FAILURE -> RANGE_RENEWAL

【设计要点】(指南第十四、十六、四十节)
- 不用单一分类器, 用状态机表达 Trend -> Range transition;
- 日K的状态转换必须更慢: 每次升级需连续 confirm_days 满足条件;
- 假突破 (High>Upper 且 Close<Upper) 不直接丢弃, 进入 BREAKOUT_FAILURE ->
  收回区间 -> RANGE_RENEWAL, 因为它可能是很好的交易机会;
- 输出 RangeState 对象 (指南第五十七章的核心数据结构)。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from range_trading.config import DailyConfig, DEFAULT_DAILY_CONFIG
from range_trading.scoring.daily_score import trend_risk_label

RANGE_STATES = {"RANGE_FORMATION", "EARLY_TRADABLE_RANGE", "MATURE_RANGE", "RANGE_RENEWAL"}


class Regime(str, Enum):
    TREND = "TREND"
    TREND_DECAY = "TREND_DECAY"
    RANGE_FORMATION = "RANGE_FORMATION"
    EARLY_TRADABLE_RANGE = "EARLY_TRADABLE_RANGE"
    MATURE_RANGE = "MATURE_RANGE"
    BREAKOUT = "BREAKOUT"
    BREAKOUT_FAILURE = "BREAKOUT_FAILURE"
    RANGE_RENEWAL = "RANGE_RENEWAL"
    # ---- 趋势工作区 (趋势指南第九章, 独立于震荡 DailyRangeStateMachine) ----
    BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"   # 突破三要素齐备 (越界+量能+站稳)
    TREND_FORMING = "TREND_FORMING"             # TQS >= 40 持续确认
    TREND_ESTABLISHED = "TREND_ESTABLISHED"     # TQS >= 60 且 Stage=2
    TREND_EXHAUSTION = "TREND_EXHAUSTION"       # 衰竭证据组合 -> 交还震荡工作区


# 趋势工作区状态集合 (Scanner/前端判定)
TREND_STATES = {"BREAKOUT_CONFIRMED", "TREND_FORMING", "TREND_ESTABLISHED", "TREND_EXHAUSTION"}


@dataclass
class RangeState:
    """单标的单日的区间状态快照 (指南第十七、五十七章输出)"""
    symbol: str
    trade_date: Any
    state: str
    age: Optional[int]                      # 区间年龄 (交易日, 自 RANGE_FORMATION 起)
    upper: float                            # DailyRangeHigh
    lower: float                            # DailyRangeLow
    mid: float
    width: float                            # 相对宽度
    close: float
    range_pos: float                        # 0=下沿 1=上沿
    di: float
    di_slope: float
    flip_rate: float
    ac1: float
    natr: float
    natr_pct: float
    upper_stability: float
    lower_stability: float
    touch: float
    support_response: float
    resistance_response: float
    vol_ratio: float                          # 量比 (突破确认 / 反转证据参考)
    vol_pct: float                            # 量能时序分位 (0~1)
    obv_slope: float                          # OBV 归一化斜率 (量价背离检测)
    vwap_dev: float                           # 量能重心相对区间位置
    range_score: float
    range_prob: float
    trend_risk: str
    trend_risk_score: float
    long_bias: float
    short_bias: float
    range_compat: float
    structure_state: str = "NEUTRAL"        # 大结构判定 (强势上升/震荡上行/底部企稳/弱势横盘/下跌)
    structural_ok: bool = False             # 是否处于允许整理买点的优质结构
    accum_state: str = "NO_SIGNAL"          # 蓄势状态 (ACCUMULATING/REVERSAL_BOUNCE/...)
    acc_score: float = 0.0                  # 蓄势评分 0~100

    def to_dict(self) -> Dict[str, Any]:
        return {
            k: (v.item() if isinstance(v, np.generic) else v)
            for k, v in self.__dict__.items()
        }


class DailyRangeStateMachine:
    """
    日K Early Range 状态机 (逐日推进)

    输入为 features + scores 合并后的 DataFrame (按 trade_date 升序),
    逐行转移状态; 升级需连续确认, 降级需连续 fallback 天不满足, 避免抖动。
    """

    def __init__(self, config: DailyConfig | None = None):
        self.cfg = config or DEFAULT_DAILY_CONFIG
        self.state = Regime.TREND
        self.range_start: int = -1            # 进入 RANGE_FORMATION 的行号
        self.renewal_start: int = -1
        # 连续计数器
        self._streak_decay: int = 0           # score >= score_trend_decay
        self._streak_formation: int = 0       # score >= score_range_formation
        self._streak_early: int = 0           # score >= score_early_range
        self._streak_weak: int = 0            # score <  score_trend_decay
        self._streak_above: int = 0           # close > upper
        self._streak_below: int = 0           # close < lower
        self._streak_inside: int = 0          # lower <= close <= upper
        self._streak_down_with_slope: int = 0 # weak 且 di_slope < 0

    # ---------- 计数器 ----------
    def _update_streaks(self, row: pd.Series) -> None:
        cfg = self.cfg
        score = row.get("daily_range_score", np.nan)
        up, lo = row.get("up"), row.get("lo")
        close = row.get("close")

        self._streak_decay = self._streak_decay + 1 if score >= cfg.score_trend_decay else 0
        self._streak_formation = (
            self._streak_formation + 1 if score >= cfg.score_range_formation else 0)
        self._streak_early = self._streak_early + 1 if score >= cfg.score_early_range else 0
        self._streak_weak = self._streak_weak + 1 if score < cfg.score_trend_decay else 0
        self._streak_down_with_slope = (
            self._streak_down_with_slope + 1
            if (score < cfg.score_trend_decay and row.get("di_slope", 0) < 0)
            else 0)

        if np.isnan(close) or np.isnan(up) or np.isnan(lo):
            self._streak_above = self._streak_below = self._streak_inside = 0
        else:
            self._streak_above = self._streak_above + 1 if close > up else 0
            self._streak_below = self._streak_below + 1 if close < lo else 0
            self._streak_inside = self._streak_inside + 1 if lo <= close <= up else 0

    # ---------- 状态转移 ----------
    def step(self, i: int, row: pd.Series) -> Regime:
        cfg = self.cfg
        self._update_streaks(row)
        s = self.state

        false_break = bool(row.get("false_bo", False) or row.get("false_bd", False))
        # 有效突破的量能确认 (指南第四十节): 突破日量比达标, 缩量突破多为假突破
        volume_confirmed = (
            cfg.breakout_volume_min <= 0
            or row.get("vol_ratio", np.nan) >= cfg.breakout_volume_min
        )

        if s in RANGE_STATES:
            if false_break:
                self.state = Regime.BREAKOUT_FAILURE          # 当日假突破事件
            elif ((self._streak_above >= cfg.breakout_confirm_days
                    or self._streak_below >= cfg.breakout_confirm_days)
                    and volume_confirmed):
                self.state = Regime.BREAKOUT                  # 有效突破
            elif self._streak_weak >= cfg.fallback_days:
                self.state = Regime.TREND_DECAY               # 区间假设失效
            elif s == Regime.RANGE_FORMATION:
                if (self._streak_early >= cfg.confirm_days
                        and row.get("touch", 0) >= cfg.min_formation_touch):
                    self.state = Regime.EARLY_TRADABLE_RANGE  # 多次反应确认
            elif s == Regime.RANGE_RENEWAL:
                if i - self.renewal_start >= cfg.renewal_hold_days:
                    if self._streak_early >= cfg.confirm_days:
                        self.state = Regime.EARLY_TRADABLE_RANGE
                    elif self._streak_formation >= cfg.confirm_days:
                        self.state = Regime.RANGE_FORMATION
                    elif self._streak_weak >= cfg.fallback_days:
                        self.state = Regime.TREND_DECAY
            else:  # EARLY / MATURE
                if s == Regime.EARLY_TRADABLE_RANGE and self._range_age(i) >= cfg.min_mature_age:
                    self.state = Regime.MATURE_RANGE
                elif self._streak_early == 0 and self._streak_weak >= cfg.fallback_days:
                    self.state = Regime.RANGE_FORMATION       # 分数回落降级

        elif s == Regime.BREAKOUT:
            if self._streak_inside >= cfg.breakout_exit_days:
                self.state = Regime.RANGE_RENEWAL             # 突破失败回归
                self.renewal_start = i
            elif self._streak_weak >= cfg.fallback_days:
                self.state = Regime.TREND                     # 突破演化为新趋势

        elif s == Regime.BREAKOUT_FAILURE:
            if self._streak_inside >= 1:
                self.state = Regime.RANGE_RENEWAL             # 收回 -> 区间更新
                self.renewal_start = i
            elif ((self._streak_above >= cfg.breakout_confirm_days
                    or self._streak_below >= cfg.breakout_confirm_days)
                    and volume_confirmed):
                self.state = Regime.BREAKOUT                  # 假突破后放量真突破
            elif self._streak_weak >= cfg.fallback_days:
                self.state = Regime.TREND_DECAY

        elif s == Regime.TREND:
            if self._streak_decay >= cfg.decay_confirm_days:
                self.state = Regime.TREND_DECAY               # DI 开始上升 / 分数抬升

        elif s == Regime.TREND_DECAY:
            if (self._streak_formation >= cfg.confirm_days
                    and row.get("width_stab", 0.0) >= cfg.min_formation_stability):
                self.state = Regime.RANGE_FORMATION           # 上下结构开始形成
                self.range_start = i
            elif self._streak_down_with_slope >= cfg.fallback_days:
                self.state = Regime.TREND                     # 趋势恢复

        if self.state == Regime.RANGE_FORMATION and self.range_start < 0:
            self.range_start = i
        return self.state

    def _range_age(self, i: int) -> int:
        return i - self.range_start if self.range_start >= 0 else 0

    def run(self, df: pd.DataFrame) -> tuple[List[str], List[Optional[int]]]:
        """逐日推进, 返回 (状态序列, 区间年龄序列)"""
        states: List[str] = []
        ages: List[Optional[int]] = []
        rows = df.to_dict("records")
        for i, rec in enumerate(rows):
            state = self.step(i, rec)
            states.append(state.value)
            if state.value in RANGE_STATES:
                ages.append(self._range_age(i))
            else:
                ages.append(None)
        return states, ages


def build_range_state(symbol: str, rec: Dict[str, Any]) -> RangeState:
    """由 (特征+分数+状态) 的一行记录构造 RangeState 快照"""
    upper, lower = float(rec["up"]), float(rec["lo"])
    mid = (upper + lower) / 2.0
    def _f(key: str, default: float = float("nan")) -> float:
        v = rec.get(key, default)
        return float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else default
    risk_score = _f("trend_cont")
    return RangeState(
        symbol=symbol,
        trade_date=rec.get("trade_date"),
        state=rec.get("state", "NA"),
        age=rec.get("age"),
        upper=upper,
        lower=lower,
        mid=mid,
        width=_f("range_width"),
        close=_f("close"),
        range_pos=_f("range_pos"),
        di=_f("di"),
        di_slope=_f("di_slope"),
        flip_rate=_f("flip"),
        ac1=_f("ac1"),
        natr=_f("natr"),
        natr_pct=_f("natr_pct"),
        upper_stability=_f("up_stab"),
        lower_stability=_f("lo_stab"),
        touch=_f("touch"),
        support_response=_f("sup_resp"),
        resistance_response=_f("res_resp"),
        vol_ratio=_f("vol_ratio"),
        vol_pct=_f("vol_pct"),
        obv_slope=_f("obv_slope"),
        vwap_dev=_f("vwap_dev"),
        range_score=_f("daily_range_score"),
        range_prob=_f("range_prob"),
        trend_risk=trend_risk_label(risk_score),
        trend_risk_score=risk_score,
        long_bias=_f("long_bias"),
        short_bias=_f("short_bias"),
        range_compat=_f("range_compat"),
        structure_state=str(rec.get("structure_state", "NEUTRAL") or "NEUTRAL"),
        structural_ok=bool(rec.get("structural_ok", False)),
        accum_state=str(rec.get("accum_state", "NO_SIGNAL") or "NO_SIGNAL"),
        acc_score=_f("acc_score", 0.0),
    )
