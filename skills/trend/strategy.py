"""Causal trend rules inspired by 趋势周期, not formulas supplied by the video.

All windows end at the current completed bar. Breakout levels explicitly exclude
that bar. Daily confirmation becomes available only at the day's 15:00 close.
Prices must already be adjusted by the data boundary; no provider calls here.
"""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from skills.trend.startup import add_startup_features

VERSION = "60f-startup-v2"


@dataclass(frozen=True)
class TrendConfig:
    selection_mode: str = "early"
    fast: int = 20
    slow: int = 60
    slope_bars: int = 8
    efficiency_bars: int = 20
    min_efficiency: float = 0.30
    min_adx: float = 20
    breakout_bars: int = 40
    min_volume_ratio: float = 1.2
    min_daily_amount: float = 30_000_000
    max_extension_atr: float = 2.5
    max_risk_pct: float = 8
    stop_atr: float = 2
    startup_body_atr: float = 0.35
    startup_reset_atr: float = 1.0
    startup_base_atr: float = 3.0
    startup_volume_ratio: float = 0.8
    startup_min_efficiency: float = 0.45
    startup_max_bars: int = 12
    startup_pause_bars: int = 2

    def __post_init__(self):
        if self.selection_mode not in ("early", "mature"):
            raise ValueError("selection_mode只支持early/mature")
        if (
            not 4 <= self.startup_max_bars <= 40
            or not 0 <= self.startup_pause_bars <= 4
        ):
            raise ValueError("启动窗口/整理根数不合法")
        if not 0 < self.startup_min_efficiency <= 1:
            raise ValueError("启动效率阈值必须在0到1之间")
        if not 2 <= self.fast < self.slow <= 240:
            raise ValueError("均线窗口应满足 2 <= fast < slow <= 240")
        if not 1 <= self.slope_bars <= 60 or not 2 <= self.efficiency_bars <= 120:
            raise ValueError("斜率/效率窗口不合法")
        if not 2 <= self.breakout_bars <= 240 or not 0 < self.min_efficiency <= 1:
            raise ValueError("突破窗口/效率阈值不合法")
        values = [
            self.min_adx,
            self.min_volume_ratio,
            self.min_daily_amount,
            self.max_extension_atr,
            self.max_risk_pct,
            self.stop_atr,
            self.startup_body_atr,
            self.startup_reset_atr,
            self.startup_base_atr,
            self.startup_volume_ratio,
        ]
        if any(not np.isfinite(v) or v <= 0 for v in values):
            raise ValueError("策略阈值必须为有限正数")


def indicators(bars: pd.DataFrame, cfg: TrendConfig = TrendConfig()) -> pd.DataFrame:
    """Ascending, deduplicated input; only past/current observations are used."""
    f = (
        bars.copy()
        .sort_values("bar_end")
        .drop_duplicates("bar_end", keep="last")
        .reset_index(drop=True)
    )
    f["bar_end"] = pd.to_datetime(f.bar_end)
    for c in ("open", "high", "low", "close", "volume", "amount"):
        f[c] = pd.to_numeric(f[c], errors="coerce")
    close = f.close
    f["ema_fast"] = close.ewm(span=cfg.fast, adjust=False, min_periods=cfg.fast).mean()
    f["ema_slow"] = close.ewm(span=cfg.slow, adjust=False, min_periods=cfg.slow).mean()
    prev = close.shift()
    tr = pd.concat(
        [f.high - f.low, (f.high - prev).abs(), (f.low - prev).abs()], axis=1
    ).max(axis=1)
    f["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    up, down = f.high.diff(), -f.low.diff()
    plus = (
        up.where((up > down) & (up > 0), 0)
        .ewm(alpha=1 / 14, adjust=False, min_periods=14)
        .mean()
    )
    minus = (
        down.where((down > up) & (down > 0), 0)
        .ewm(alpha=1 / 14, adjust=False, min_periods=14)
        .mean()
    )
    denom = (plus + minus).replace(0, np.nan)
    f["adx"] = (
        (100 * (plus - minus).abs() / denom)
        .ewm(alpha=1 / 14, adjust=False, min_periods=14)
        .mean()
        .fillna(0)
    )
    path = close.diff().abs().rolling(cfg.efficiency_bars).sum()
    f["efficiency"] = (close - close.shift(cfg.efficiency_bars)).abs() / path.replace(
        0, np.nan
    )
    f["efficiency"] = f.efficiency.fillna(0)
    f["prior_high"] = f.high.shift().rolling(cfg.breakout_bars).max()
    f["volume_ratio"] = f.volume / f.volume.shift().rolling(20).mean().replace(
        0, np.nan
    )
    f["slow_slope"] = f.ema_slow / f.ema_slow.shift(cfg.slope_bars) - 1
    f["extension_atr"] = (close - f.ema_fast) / f.atr.replace(0, np.nan)
    f["breakout"] = (close > f.prior_high) & (f.volume_ratio >= cfg.min_volume_ratio)
    # A pullback touches a rising EMA without closing below the slow trend;
    # confirmation requires an up close and a higher close than the prior bar.
    touch = (
        ((f.low <= f.ema_fast + 0.5 * f.atr) & (f.close >= f.ema_slow))
        .rolling(4)
        .max()
        .shift()
        .fillna(0)
        .astype(bool)
    )
    f["pullback"] = touch & (close > f.ema_fast) & (close > prev) & (close > f.open)
    daily = (
        f.set_index("bar_end")
        .resample("D")
        .agg(
            close=("close", "last"), amount=("amount", "sum"), count=("close", "count")
        )
    )
    daily = daily[daily["count"] == 4].copy()
    daily["daily_fast"] = daily.close.ewm(
        span=cfg.fast, adjust=False, min_periods=cfg.fast
    ).mean()
    daily["daily_slow"] = daily.close.ewm(
        span=cfg.slow, adjust=False, min_periods=cfg.slow
    ).mean()
    daily["daily_slope"] = daily.daily_slow.diff(3)
    daily["daily_amount"] = daily.amount.rolling(20).mean()
    daily.index = daily.index + pd.Timedelta(hours=15)
    f = pd.merge_asof(
        f,
        daily[["daily_fast", "daily_slow", "daily_slope", "daily_amount"]],
        left_on="bar_end",
        right_index=True,
        direction="backward",
    )
    f["daily_trend"] = (f.daily_fast > f.daily_slow) & (f.daily_slope > 0)
    return add_startup_features(f, cfg)


def _checks(f: pd.DataFrame, cfg: TrendConfig):
    early = cfg.selection_mode == "early"
    return [
        ("daily", "日线顺势", f.daily_trend, "完整日线EMA快线高于慢线，慢线3日上行"),
        (
            "trend",
            "启动转强" if early else "60F多头",
            (
                (f.close > f.ema_fast)
                & (f.ema_fast.diff() > 0)
                & (f.close > f.startup_origin)
            )
            if early
            else (
                (f.close > f.ema_fast) & (f.ema_fast > f.ema_slow) & (f.slow_slope > 0)
            ),
            "收盘重上EMA快线且快线转升，不强等慢线金叉"
            if early
            else "收盘 > EMA快 > EMA慢，慢线斜率为正",
        ),
        (
            "smooth",
            "启动推进" if early else "趋势流畅",
            f.startup_efficiency >= cfg.startup_min_efficiency
            if early
            else (f.efficiency >= cfg.min_efficiency) & (f.adx >= cfg.min_adx),
            f"本段价格净推进/实际路径≥{cfg.startup_min_efficiency:g}"
            if early
            else f"ER≥{cfg.min_efficiency:g}，ADX≥{cfg.min_adx:g}",
        ),
        (
            "liquidity",
            "流动性",
            f.daily_amount >= cfg.min_daily_amount,
            f"20日日均成交额≥{cfg.min_daily_amount / 1e8:g}亿",
        ),
        (
            "extension",
            "不过度追高",
            (f.extension_atr <= cfg.max_extension_atr)
            if early
            else f.extension_atr.between(0, cfg.max_extension_atr),
            f"距快线不超过{cfg.max_extension_atr:g}ATR",
        ),
        (
            "risk",
            "风险边界",
            (f.stop_price > 0) & f.risk_pct.gt(0) & f.risk_pct.le(cfg.max_risk_pct),
            f"结构低点外且至少{cfg.stop_atr:g}ATR，距离≤{cfg.max_risk_pct:g}%",
        ),
        (
            "trigger",
            "第2—4阳确认" if early else "入场确认",
            (f.startup_advancing & f.startup_early & f.startup_count.between(2, 4))
            if early
            else (f.breakout | f.pullback),
            f"同段第2—4根推进阳线，启动≤{cfg.startup_max_bars}根；首阳仅观察"
            if early
            else f"放量突破前{cfg.breakout_bars}根高点，或回踩快线后转强",
        ),
    ]


def evaluated_bars(bars: pd.DataFrame, cfg: TrendConfig = TrendConfig()):
    """One causal decision path shared by latest screening and historical markers."""
    f = indicators(bars, cfg)
    low = f.startup_low if cfg.selection_mode == "early" else f.low.rolling(8).min()
    f["stop_price"] = np.minimum(low - 0.2 * f.atr, f.close - cfg.stop_atr * f.atr)
    f["risk_pct"] = (f.close - f.stop_price) / f.close * 100
    warm = f[["ema_slow", "daily_slow", "daily_amount", "slow_slope"]].notna().all(
        axis=1
    ) & f.atr.gt(0)
    checks = _checks(f, cfg)
    f["eligible"] = (
        pd.concat([c[2] for c in checks], axis=1).fillna(False).all(axis=1) & warm
    )
    # First bars below EMA20 can be observed without pretending they are confirmed.
    f["first_watch"] = (
        warm & f.startup_early & f.startup_advancing & f.startup_count.eq(1)
    )
    f["first_watch"] &= (
        f.daily_trend
        & (f.daily_amount >= cfg.min_daily_amount)
        & (f.extension_atr <= cfg.max_extension_atr)
    )
    f["first_watch"] &= (f.stop_price > 0) & f.risk_pct.between(
        0, cfg.max_risk_pct, inclusive="right"
    )
    if cfg.selection_mode != "early":
        f["first_watch"] = False
    return f


def screen(bars: pd.DataFrame, cfg: TrendConfig = TrendConfig()) -> dict:
    """Return explicit gates as well as a score; score cannot bypass a failed gate."""
    if bars.empty:
        return {
            "state": "数据不足",
            "eligible": False,
            "score": 0,
            "checks": [],
            "reason": "没有60F数据",
        }
    f = evaluated_bars(bars, cfg)
    r = f.iloc[-1]
    required = [
        "ema_fast",
        "ema_slow",
        "atr",
        "prior_high",
        "slow_slope",
        "daily_amount",
        "daily_slow",
    ]
    if any(pd.isna(r[k]) for k in required) or r.atr <= 0:
        return {
            "state": "数据不足",
            "eligible": False,
            "score": 0,
            "checks": [],
            "reason": "需要至少63个完整交易日及有效波动",
        }
    stop, risk = float(r.stop_price), float(r.risk_pct)
    checks = [
        (key, label, bool(mask.iloc[-1]), rule)
        for key, label, mask, rule in _checks(f, cfg)
    ]
    failed = [label for _, label, ok, _ in checks if not ok]
    eligible = not failed
    if eligible:
        state = (
            f"启动第{int(r.startup_count)}阳"
            if cfg.selection_mode == "early"
            else "突破候选"
            if r.breakout
            else "回踩候选"
        )
    elif r.first_watch:
        state = "首阳观察"
    elif cfg.selection_mode == "early" and r.daily_trend and r.startup_count == 0:
        state = "等待启动"
    elif not checks[0][2] or not checks[1][2]:
        state = "弱势过滤"
    elif cfg.selection_mode == "early" and r.startup_count > 4:
        state = "启动已过"
    elif not checks[2][2]:
        state = "震荡过滤"
    elif not checks[4][2]:
        state = "追高过滤"
    else:
        state = "趋势观察"
    # Ranking quality only, not a win probability or a return forecast.
    early = cfg.selection_mode == "early"
    efficiency = float(r.startup_efficiency if early else r.efficiency)
    timing_quality = (
        max(0.0, (5 - int(r.startup_count)) / 3) if r.startup_count >= 2 else 0.0
    )
    if not early:
        timing_quality = min(float(r.adx) / 50, 1.0)
    risk_quality = max(0.0, 1 - risk / cfg.max_risk_pct) if np.isfinite(risk) else 0.0
    score = round(
        40 * sum(x[2] for x in checks) / len(checks)
        + 30 * min(efficiency, 1.0)
        + 20 * timing_quality
        + 10 * risk_quality,
        1,
    )
    metrics = {
        k: round(float(r[k]), 6) if pd.notna(r[k]) and np.isfinite(r[k]) else None
        for k in [
            "close",
            "ema_fast",
            "ema_slow",
            "atr",
            "adx",
            "efficiency",
            "slow_slope",
            "volume_ratio",
            "extension_atr",
            "prior_high",
            "daily_amount",
        ]
    }
    return {
        "state": state,
        "eligible": eligible,
        "score": score,
        "reason": "全部条件通过"
        if eligible
        else "首阳仅观察，等待第2—4阳确认"
        if r.first_watch
        else " / ".join(failed),
        "checks": [
            {"key": k, "label": label, "passed": ok, "rule": rule}
            for k, label, ok, rule in checks
        ],
        "metrics": metrics,
        "stop_price": round(stop, 4) if np.isfinite(stop) else None,
        "risk_pct": round(risk, 3) if np.isfinite(risk) else None,
        "startup": {
            "start": r.startup_start.isoformat() if pd.notna(r.startup_start) else None,
            "bullish_count": int(r.startup_count),
            "age_bars": int(r.startup_age),
            "efficiency": round(float(r.startup_efficiency), 4),
            "early": bool(r.startup_early),
        },
        "bar_end": r.bar_end.isoformat(),
        "config": asdict(cfg),
    }
