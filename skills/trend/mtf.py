"""Completed 60F trend + native daily entry. M8/M13/M21 are simple averages.

The name 'institutional lines' describes the requested periods, not observable
institutional orders. Daily decisions see only that day's completed 60F bars.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

VERSION = "60f-trend-daily-entry-v5.1"
REVERSAL_VERSION = "60f-early-reversal-daily-entry-v6"
FRESH_VERSION = "60f-early-reversal-daily-entry-v6.1"
DUAL_VERSION = "60f-risk-structured-setups-v7"
LABELS = {"trend_pullback": "趋势回调", "base_rebound": "止跌修复"}


@dataclass(frozen=True)
class MtfConfig:
    selection_mode: str = "mtf"
    fast: int = 8
    mid: int = 13
    slow: int = 21
    min_daily_amount: float = 30_000_000
    daily_body_atr: float = 0.5
    min_volume_ratio: float = 0.8
    prior_volume_ratio: float = 1.1
    trend_flat_atr: float = 0.25
    stop_atr: float = 1.25
    max_risk_pct: float = 5
    max_extension_atr: float = 2.5
    cooldown_days: int = 5
    slope_epsilon_atr: float = 1e-8
    # Opt in explicitly so persisted v5.1 configurations remain reproducible.
    fresh_reversal: bool = False
    reversal_cycle_reset: bool = False
    risk_setups: bool = False

    def __post_init__(self):
        if type(self.risk_setups) is not bool or (
            self.risk_setups and not self.fresh_reversal
        ):
            raise ValueError("risk_setups需要早期反转模式及布尔参数")
        if type(self.fresh_reversal) is not bool:
            raise ValueError("fresh_reversal必须为布尔值")
        if type(self.reversal_cycle_reset) is not bool:
            raise ValueError("reversal_cycle_reset必须为布尔值")
        if self.reversal_cycle_reset and not self.fresh_reversal:
            raise ValueError("新下降周期退役只适用于早期反转模式")
        if self.fresh_reversal and (self.fast, self.mid, self.slow) != (8, 13, 21):
            raise ValueError("早期反转模式使用固定M8/M13/M21")
        if self.selection_mode != "mtf":
            raise ValueError("MtfConfig只接受mtf模式")
        if any(
            type(x) is not int
            for x in [self.fast, self.mid, self.slow, self.cooldown_days]
        ):
            raise ValueError("均线周期/冷却期必须为整数")
        if not 2 <= self.fast < self.mid < self.slow <= 120 or self.cooldown_days < 1:
            raise ValueError("均线周期/冷却期不合法")
        for key in [
            "min_daily_amount",
            "daily_body_atr",
            "min_volume_ratio",
            "prior_volume_ratio",
            "trend_flat_atr",
            "stop_atr",
            "max_risk_pct",
            "max_extension_atr",
        ]:
            if not math.isfinite(getattr(self, key)) or getattr(self, key) <= 0:
                raise ValueError("策略阈值必须为有限正数")
        if not math.isfinite(self.slope_epsilon_atr) or self.slope_epsilon_atr < 0:
            raise ValueError("斜率容差必须为有限非负数（0仅供旧版本回放）")


def version_for(cfg):
    if cfg.risk_setups:
        return DUAL_VERSION
    return (
        (FRESH_VERSION if cfg.reversal_cycle_reset else REVERSAL_VERSION)
        if cfg.fresh_reversal
        else VERSION
    )


def features(bars, cfg=MtfConfig()):
    f = bars.sort_values("bar_end").drop_duplicates("bar_end").reset_index(drop=True)
    f["bar_end"] = pd.to_datetime(f.bar_end)
    for key in ["open", "high", "low", "close", "volume", "amount"]:
        if not pd.api.types.is_numeric_dtype(f[key]):
            f[key] = pd.to_numeric(f[key], errors="coerce")
    for period in [cfg.fast, cfg.mid, cfg.slow]:
        f[f"ma{period}"] = f.close.rolling(period).mean()
    previous = f.close.shift()
    tr = pd.concat(
        [f.high - f.low, (f.high - previous).abs(), (f.low - previous).abs()], axis=1
    ).max(axis=1)
    f["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    f["volume_ratio"] = f.volume / f.volume.shift().rolling(20).mean().replace(
        0, np.nan
    )
    return f


def minute_context(minute, cfg=MtfConfig()):
    """The same causal 60F context used by daily decisions and intraday review."""
    m = features(minute, cfg)
    fast, mid, slow = [m[f"ma{n}"] for n in [cfg.fast, cfg.mid, cfg.slow]]
    eps = cfg.slope_epsilon_atr * m.atr
    # Economic prices are scale invariant. A flat MA can differ by 1 ULP after
    # rebasing factors; do not turn that numerical noise into a trend decision.
    orderly = (
        (fast - mid).gt(eps)
        & (mid - slow).gt(eps)
        & slow.diff(4).ge(-eps)
        & mid.diff(2).gt(eps)
    )
    repair = (
        (fast - mid).gt(eps)
        & (m.close >= slow)
        & fast.diff(2).gt(eps)
        & slow.diff(4).ge(-cfg.trend_flat_atr * m.atr)
        & mid.diff().ge(-eps)
    )
    m["trend_ready"] = ((orderly | repair) & m.close.ge(mid - 0.5 * m.atr)).fillna(
        False
    )
    m["trend_state"] = np.select(
        [orderly, repair], ["趋势向上", "止跌修复"], default="趋势未确认"
    )
    m["continuation_ready"] = m.trend_ready
    m["early_repair_ready"] = (m.close.ge(fast) & fast.diff(2).gt(eps)).fillna(False)
    if cfg.fresh_reversal:
        from skills.trend.reversal import reversal_context

        m = reversal_context(m, cycle_reset=cfg.reversal_cycle_reset)
        m["trend_ready"] = (
            m.reversal_fresh
            & m.close.ge(m[f"ma{cfg.fast}"])
            & m[f"ma{cfg.fast}"].diff().gt(eps)
        ).fillna(False)
        m["trend_state"] = m.reversal_state
    return m


def evaluate(minute, daily, cfg=MtfConfig()):
    """Input prices must be adjusted; outputs never create a 60F entry order."""
    m, d = minute_context(minute, cfg), features(daily, cfg)
    reversal_cols = (
        [c for c in m if c.startswith("reversal_")] if cfg.fresh_reversal else []
    )
    # A stale previous-day context, or a day with a missing 60F bar, cannot trade.
    count = m.groupby(m.bar_end.dt.normalize()).bar_end.transform("size")
    complete = m.bar_end.dt.hour.eq(15) & m.bar_end.dt.minute.eq(0) & count.eq(4)
    context = m.loc[
        complete,
        [
            "bar_end",
            "trend_ready",
            "trend_state",
            "continuation_ready",
            "early_repair_ready",
            f"ma{cfg.fast}",
            f"ma{cfg.mid}",
            f"ma{cfg.slow}",
            *reversal_cols,
        ],
    ].rename(
        columns={
            "bar_end": "context_end",
            **{f"ma{n}": f"context_ma{n}" for n in [cfg.fast, cfg.mid, cfg.slow]},
        }
    )
    d = pd.merge_asof(
        d, context, left_on="bar_end", right_on="context_end", direction="backward"
    )
    d["context_complete"] = d.context_end.eq(d.bar_end)
    if cfg.fresh_reversal:
        d["reversal_fresh"] = (
            d.reversal_fresh.fillna(False).astype(bool) & d.context_complete
        )
    d["trend_ready"] = d.trend_ready.fillna(False).astype(bool) & d.context_complete
    d["trend_state"] = d.trend_state.fillna("60F数据不足").where(
        d.context_complete, "60F数据不足"
    )
    atr = d.atr.shift()
    previous = d.close.shift()
    daily_mid, daily_slow = d[f"ma{cfg.mid}"], d[f"ma{cfg.slow}"]
    prior_up = (
        (
            (daily_mid - daily_slow).gt(cfg.slope_epsilon_atr * d.atr)
            & daily_slow.diff(3).gt(cfg.slope_epsilon_atr * d.atr)
        )
        .shift(3)
        .rolling(20, min_periods=1)
        .max()
        .fillna(0)
        .gt(0)
    )
    drawdown = d.high.shift().rolling(10).max() - previous
    touch = (
        ((d.low <= daily_mid + 0.3 * d.atr) | (d.low <= daily_slow + 0.3 * d.atr))
        .shift()
        .rolling(3)
        .max()
        .fillna(0)
        .gt(0)
    )
    controlled = previous.ge(daily_slow.shift() - atr) & drawdown.between(
        0.4 * atr, 4 * atr
    )
    context_a = prior_up & controlled & touch
    path = d.close.diff().abs().shift().rolling(8).sum()
    er = (previous - d.close.shift(9)).abs() / path.replace(0, np.nan)
    drift = previous.lt(d.close.shift(6))
    base = (d.high.shift().rolling(8).max() - d.low.shift().rolling(8).min()).le(
        3 * atr
    ) & er.fillna(0).le(0.45)
    near_floor = previous - d.low.shift().rolling(10).min() <= 3 * atr
    context_b = (drift | base) & near_floor
    if cfg.fresh_reversal:
        # The precondition is now the completed 60F decline/pit, rather than
        # permitting a mature daily uptrend to supply an unrelated background.
        context_a = pd.Series(False, index=d.index)
        context_b = d.reversal_fresh.fillna(False)
    if cfg.risk_setups:
        from skills.trend.pullback import controlled_pullbacks

        pullback = controlled_pullbacks(d)
        d["pullback_round"] = pullback["rounds"]
        context_a = pullback.active & touch & d.context_complete
        d["trend_ready"] |= (
            context_a & d.continuation_ready.fillna(False) & d.context_complete
        )
        d.loc[context_a & d.trend_ready, "trend_state"] = "趋势回调转强"
    body = d.close - d.open
    location = (d.close - d.low) / (d.high - d.low).replace(0, np.nan)
    red = (d.close < d.open).shift().rolling(3).sum().ge(2)
    # Reclaim prior bearish bodies; requiring all prior wicks misses repair bars.
    engulf = d.close.ge(d.open.shift().rolling(2).max()) & red
    turn = d.close.gt(previous) & (d.close.gt(d.high.shift()) | engulf)
    volume = d.volume_ratio.ge(cfg.min_volume_ratio) | d.volume.ge(
        d.volume.shift() * cfg.prior_volume_ratio
    )
    d["quality_candle"] = (
        body.ge(cfg.daily_body_atr * atr)
        & location.ge(0.7)
        & turn
        & volume
        & d.close.ge(d[f"ma{cfg.fast}"] - 0.1 * atr)
    ).fillna(False)
    if cfg.risk_setups:
        d["gap_held"] = (
            (d.open - d.high.shift()).ge(0.5 * atr)
            & (d.open - previous).le(2 * atr)
            & d.low.gt(d.high.shift())
            & d.close.ge(d.open)
            & volume
        ).fillna(False)
        d["quality_candle"] |= d.gap_held
        gap_context = d.gap_held & drift & near_floor & d.context_complete
        context_b |= gap_context
        d["trend_ready"] |= gap_context & d.early_repair_ready.fillna(False)
        d.loc[gap_context & d.trend_ready, "trend_state"] = "缺口止跌修复"
    else:
        d["gap_held"] = False
    d["daily_amount"] = d.amount.rolling(20).mean()
    d["extension_atr"] = (d.close - daily_mid) / d.atr
    d["stop_price"] = np.minimum(
        d.low.rolling(3).min() - 0.15 * atr, d.close - cfg.stop_atr * atr
    )
    if cfg.risk_setups:
        # An unfilled gap has a distinct invalidation structure. Do not borrow
        # a much older low, nor remove the minimum one-ATR volatility allowance.
        d.loc[d.gap_held, "stop_price"] = np.minimum(d.low - 0.25 * atr, d.close - atr)[
            d.gap_held
        ]
    d["risk_pct"] = (d.close - d.stop_price) / d.close * 100
    d["liquidity_ok"] = d.daily_amount.ge(cfg.min_daily_amount)
    d["risk_ok"] = d.stop_price.gt(0) & d.risk_pct.between(
        0, cfg.max_risk_pct, inclusive="right"
    )
    d["extension_ok"] = d.extension_atr.le(cfg.max_extension_atr)
    ready = d[[f"ma{cfg.slow}", "atr", "daily_amount"]].notna().all(axis=1)
    d["a_context"], d["b_context"] = context_a.fillna(False), context_b.fillna(False)
    raw = (
        ready
        & d.trend_ready
        & d.quality_candle
        & d.liquidity_ok
        & d.risk_ok
        & d.extension_ok
        & (context_a | context_b)
    )
    last = -cfg.cooldown_days
    accepted = []
    for i, trigger in enumerate(raw):
        allowed = bool(trigger and i - last >= cfg.cooldown_days)
        accepted.append(allowed)
        if allowed:
            last = i
    d["eligible"] = accepted
    d["a_eligible"] = d.eligible & context_a
    d["b_eligible"] = d.eligible & ~context_a & context_b
    d["pattern"] = np.where(
        context_a, "trend_pullback", np.where(context_b, "base_rebound", "")
    )
    d["pattern_label"] = d.pattern.map(LABELS).fillna("等待结构")
    d["state"] = np.select(
        [d.eligible, ~ready, d.trend_ready],
        ["日K入场候选", "数据不足", "等待日K确认"],
        default="60F趋势未确认",
    )
    if cfg.fresh_reversal:
        d.loc[~d.eligible & d.reversal_fresh.fillna(False), "state"] = "早期反转观察"
        d.loc[~d.eligible & d.reversal_state.eq("反转已过早期"), "state"] = (
            "反转已过早期"
        )
        d.loc[~d.context_complete, "state"] = "数据不足"
        d["pattern_label"] = d.reversal_kind.fillna("").replace("", "等待低位反转")
        if cfg.risk_setups:
            d.loc[context_a, "pattern_label"] = "趋势首次回调"
            d.loc[context_a & d.pullback_round.gt(1), "pattern_label"] = "趋势回调再起"
            d.loc[d.gap_held & context_b & ~context_a, "pattern_label"] = "低位缺口守稳"
            d.loc[context_a & ~d.eligible, "state"] = "首次回调观察"
    d["score"] = (
        40 * d.trend_ready.astype(int)
        + 20 * d.quality_candle.astype(int)
        + 20 * d.risk_ok.astype(int)
        + 10 * d.liquidity_ok.astype(int)
        + 10 * (1 - d.risk_pct / cfg.max_risk_pct).clip(0, 1)
    ).round(1)
    if cfg.fresh_reversal:
        d.loc[
            ~(d.reversal_fresh.fillna(False) | context_a | (d.gap_held & context_b)),
            "score",
        ] = 0
    return m, d


def result(d, cfg=MtfConfig()):
    r = d.iloc[-1]
    checks = [
        (
            "trend60",
            "60F趋势",
            r.trend_ready,
            f"完整60F的M{cfg.fast}/M{cfg.mid}/M{cfg.slow}多头或止跌修复；无缺根",
        ),
        (
            "structure",
            "日K前置结构",
            r.a_context or r.b_context,
            "原升势受控回调，或阴跌/盘整靠近支撑",
        ),
        (
            "candle",
            "日K转强阳线",
            r.quality_candle,
            f"实体≥{cfg.daily_body_atr}ATR、收于上部30%、收复前高或阴线实体并有量能支持",
        ),
        (
            "liquidity",
            "流动性",
            r.liquidity_ok,
            f"20日日均成交额≥{cfg.min_daily_amount / 1e8:g}亿",
        ),
        (
            "extension",
            "不过度追高",
            r.extension_ok,
            f"距日K M{cfg.mid}≤{cfg.max_extension_atr}ATR",
        ),
        (
            "risk",
            "初始风险",
            r.risk_ok,
            f"结构低点外且至少{cfg.stop_atr}ATR，距离≤{cfg.max_risk_pct}%",
        ),
        (
            "trigger",
            "新一轮入场",
            r.eligible,
            f"日K收盘确认；同股至少间隔{cfg.cooldown_days}个交易日",
        ),
    ]
    if cfg.fresh_reversal:
        checks[0] = (
            "trend60",
            "60F早期反转",
            r.trend_ready,
            "阴跌/深坑后的有效首阳；M8转升并收复M8；限起点后12根、前4根推进阳线、距起点≤3ATR，距M21≤2ATR",
        )
        checks[1] = (
            "structure",
            "60F低位前置结构",
            r.b_context,
            "前16根下跌≥2ATR且至少半数下跌，或前8根回撤≥3ATR；处64根区间下45%，靠近16根低点且M13此前下降",
        )

    if cfg.risk_setups:
        checks[0] = (
            "trend60",
            "60F方向确认",
            r.trend_ready,
            "低位反转修复或首次回调后的60F趋势恢复；当天完整收盘背景",
        )
        checks[1] = (
            "structure",
            "机会前置结构",
            r.a_context or r.b_context,
            "低位阴跌/深坑，或已有升势的受控回调（首次与后续分别标注）；回调不冒充首阳",
        )
        checks[2] = (
            "candle",
            "日K转强或缺口守稳",
            r.quality_candle,
            "转强阳线，或高开超过前高0.5ATR、全天不补缺口且收阳；高开幅度不超过2ATR并有量能",
        )
        checks[5] = (
            "risk",
            "初始风险",
            r.risk_ok,
            "常规结构止损；缺口守稳按当日低点外0.25ATR且至少1ATR缓冲；风险距离不超过配置上限",
        )

    def num(key):
        value = r.get(key, np.nan)
        return (
            round(float(value), 6) if pd.notna(value) and np.isfinite(value) else None
        )

    return {
        "state": r.state,
        "eligible": bool(r.eligible),
        "score": num("score") or 0,
        "pattern": r.pattern,
        "pattern_label": r.pattern_label,
        "setup_watch": bool(r.a_context or r.b_context)
        if cfg.risk_setups
        else bool(r.get("reversal_fresh", False)),
        "pullback_round": num("pullback_round"),
        "timeframe": "1D",
        "bar_end": r.bar_end.isoformat(),
        "reason": "60F趋势与日K转强确认，下一交易日开盘尝试入场"
        if r.eligible
        else " / ".join(label for _, label, ok, _ in checks if not ok),
        "checks": [
            {"key": k, "label": label, "passed": bool(ok), "rule": rule}
            for k, label, ok, rule in checks
        ],
        "metrics": {
            k: num(k)
            for k in [
                "close",
                f"ma{cfg.fast}",
                f"ma{cfg.mid}",
                f"ma{cfg.slow}",
                "atr",
                "volume_ratio",
                "extension_atr",
                "daily_amount",
            ]
        },
        "stop_price": num("stop_price"),
        "risk_pct": num("risk_pct"),
        "trend60": {
            "ready": bool(r.trend_ready),
            "state": r.trend_state,
            "bar_end": r.context_end.isoformat() if pd.notna(r.context_end) else None,
            **{f"ma{n}": num(f"context_ma{n}") for n in [cfg.fast, cfg.mid, cfg.slow]},
            **(
                {
                    "origin": r.reversal_start.isoformat()
                    if pd.notna(r.reversal_start)
                    else None,
                    "age_bars": num("reversal_age"),
                    "bullish_count": num("reversal_bulls"),
                    "gain_atr": num("reversal_gain_atr"),
                    "floor": num("reversal_floor"),
                    "kind": r.reversal_kind if pd.notna(r.reversal_kind) else "",
                    "fresh": bool(r.reversal_fresh)
                    if pd.notna(r.reversal_fresh)
                    else False,
                }
                if cfg.fresh_reversal
                else {}
            ),
        },
    }
