"""Causal trend rules inspired by 趋势周期, not formulas supplied by the video.

All windows end at the current completed bar. Breakout levels explicitly exclude
that bar. Daily confirmation becomes available only at the day's 15:00 close.
Prices must already be adjusted by the data boundary; no provider calls here.
"""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

VERSION = "60f-trend-v1"


@dataclass(frozen=True)
class TrendConfig:
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

    def __post_init__(self):
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
    f = indicators(bars, cfg)
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
    structural_stop = float(f.low.tail(8).min() - 0.2 * r.atr)
    # Give the hourly structure room: stop is outside BOTH the recent low and
    # the ATR distance. If that is too far away, reject instead of tightening it.
    stop = min(structural_stop, float(r.close - cfg.stop_atr * r.atr))
    risk = max(0.0, (float(r.close) - stop) / float(r.close) * 100)
    checks = [
        ("daily", "日线顺势", bool(r.daily_trend), "日线EMA快线高于慢线，慢线3日上行"),
        (
            "trend",
            "60F多头",
            bool(r.close > r.ema_fast > r.ema_slow and r.slow_slope > 0),
            "收盘 > EMA快 > EMA慢，慢线斜率为正",
        ),
        (
            "smooth",
            "趋势流畅",
            bool(r.efficiency >= cfg.min_efficiency and r.adx >= cfg.min_adx),
            f"ER≥{cfg.min_efficiency:g}，ADX≥{cfg.min_adx:g}",
        ),
        (
            "liquidity",
            "流动性",
            bool(r.daily_amount >= cfg.min_daily_amount),
            f"20日日均成交额≥{cfg.min_daily_amount / 1e8:g}亿",
        ),
        (
            "extension",
            "不过度追高",
            bool(0 <= r.extension_atr <= cfg.max_extension_atr),
            f"距快线不超过{cfg.max_extension_atr:g}ATR",
        ),
        (
            "risk",
            "风险边界",
            bool(stop > 0 and 0 < risk <= cfg.max_risk_pct),
            f"结构低点外且至少{cfg.stop_atr:g}ATR，距离≤{cfg.max_risk_pct:g}%",
        ),
        (
            "trigger",
            "入场确认",
            bool(r.breakout or r.pullback),
            f"放量突破前{cfg.breakout_bars}根高点，或回踩快线后转强",
        ),
    ]
    failed = [label for _, label, ok, _ in checks if not ok]
    eligible = not failed
    if eligible:
        state = "突破候选" if r.breakout else "回踩候选"
    elif not checks[0][2] or not checks[1][2]:
        state = "弱势过滤"
    elif not checks[2][2]:
        state = "震荡过滤"
    elif not checks[4][2]:
        state = "追高过滤"
    else:
        state = "趋势观察"
    # Ranking quality only, not a win probability or a return forecast.
    score = round(
        40 * sum(x[2] for x in checks) / len(checks)
        + 30 * min(float(r.efficiency), 1)
        + 20 * min(float(r.adx) / 50, 1)
        + 10 * max(0, 1 - risk / cfg.max_risk_pct),
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
        "reason": "全部条件通过" if eligible else " / ".join(failed),
        "checks": [
            {"key": k, "label": label, "passed": ok, "rule": rule}
            for k, label, ok, rule in checks
        ],
        "metrics": metrics,
        "stop_price": round(stop, 4),
        "risk_pct": round(risk, 3),
        "bar_end": r.bar_end.isoformat(),
        "config": asdict(cfg),
    }
