"""Two independent long-only hypotheses on completed 60F or daily bars.

A: re-enter an intact rising trend after a controlled pullback.
B: repair a sideways drift or reclaim a previously broken platform after a pit.
B deliberately has no bullish daily-MA requirement. These are screening rules,
not a claim that a rebound has become a lasting trend reversal.
"""

import numpy as np
import pandas as pd
from skills.trend.legs import track_leg

LABELS = {"trend_pullback": "趋势回调", "base_rebound": "阴跌/砸坑反弹"}


def _evidence(signs):
    out = pd.Series("", index=signs.index)
    for label, mask in signs.items():
        out += np.where(mask.fillna(False), label + " / ", "")
    return out.str.rstrip(" / ")


def evaluate_setups(f, cfg, timeframe):
    f = f.copy()
    atr = f.atr.shift()
    prev = f.close.shift()
    high20 = f.high.shift().rolling(20).max()
    low8 = f.low.shift().rolling(8).min()
    high8 = f.high.shift().rolling(8).max()
    high4 = f.high.shift().rolling(4).max()
    floor = f.low.shift(5).rolling(8).min()
    holds = f.low.shift().rolling(4).min() >= floor - 0.2 * atr
    higher_low = (
        f.low.shift().rolling(2).min() > f.low.shift(3).rolling(2).min() + 0.1 * atr
    )
    tr = pd.concat(
        [f.high - f.low, (f.high - prev).abs(), (f.low - prev).abs()], axis=1
    ).max(axis=1)
    quieter = tr.shift().rolling(4).mean() <= 0.9 * tr.shift(5).rolling(4).mean()
    selling = (f.open - f.close).clip(lower=0)
    old_selling = selling.shift(5).rolling(4).sum()
    easing = (old_selling > 0.1 * atr) & (
        selling.shift().rolling(4).sum() <= 0.7 * old_selling
    )
    dry_volume = (
        f.volume.shift().rolling(4).mean() <= 0.9 * f.volume.shift(5).rolling(4).mean()
    )
    repair_signs = pd.DataFrame(
        {
            "低点抬高": higher_low,
            "波动收敛": quieter,
            "阴线实体减弱": easing,
            "回调缩量": dry_volume,
        }
    ).fillna(False)
    path8 = f.close.diff().abs().shift().rolling(8).sum()
    er8 = (prev - f.close.shift(9)).abs() / path8.replace(0, np.nan)
    er8 = er8.fillna(0)
    base = (high8 - low8 <= cfg.startup_base_atr * atr) & (er8 <= 0.35)
    drift = (f.close.shift(13) - prev).between(0.5 * atr, 6 * atr)
    drawdown = high20 - prev
    # Trend evidence is measured BEFORE the last four pullback bars.
    trend_history = (
        (f.ema_fast > f.ema_slow)
        & f.ema_slow.diff(8).gt(0)
        & ((f.close - f.close.shift(20)) >= 2 * f.atr)
    )
    prior_up = trend_history.shift(4).rolling(20).max().fillna(0).gt(0)
    intact = prev >= f.ema_slow.shift() - atr
    higher_context = (
        f.daily_trend if timeframe == "60F" else pd.Series(True, index=f.index)
    )
    context_a = prior_up & intact & higher_context & drawdown.between(atr, 6 * atr)
    prepared_a = context_a & holds & repair_signs.any(axis=1)
    evidence_a = _evidence(
        pd.DataFrame(
            {
                "回调前升势成立": prior_up,
                "原趋势未破": intact,
                **{k: v for k, v in repair_signs.items()},
            }
        )
    )
    # B1: base/drift repair. The prior daily trend may still be bearish.
    base_signs = pd.DataFrame(
        {
            "低点抬高": higher_low,
            "波动收敛": quieter,
            "阴线实体减弱": easing,
            "底部盘整": base,
        }
    ).fillna(False)
    prepared_base = (
        holds & (base | drift) & base_signs.sum(axis=1).ge(cfg.precursor_min_signs)
    )
    evidence_base = _evidence(base_signs)
    # B2: a platform must exist before the pit; a naked plunge is insufficient.
    platform_low = f.low.shift(5).rolling(12).min()
    platform_high = f.high.shift(5).rolling(12).max()
    old_path = f.close.diff().abs().shift(5).rolling(12).sum()
    platform_er = (f.close.shift(5) - f.close.shift(17)).abs() / old_path.replace(
        0, np.nan
    )
    platform_er = platform_er.fillna(0)
    platform = (platform_high - platform_low <= 6 * atr) & (platform_er <= 0.45)
    pit_low = f.low.shift().rolling(4).min()
    depth = (platform_low - pit_low) / atr.replace(0, np.nan)
    pit = platform & depth.between(1, 4)
    location = (f.close - f.low) / (f.high - f.low).replace(0, np.nan)
    ignition = (
        (f.close > f.open)
        & ((f.close - f.open) >= cfg.startup_body_atr * atr)
        & (f.close > prev)
        & (f.close > f.high.shift())
    )
    ignition &= (location >= 0.65) & (f.volume_ratio >= cfg.startup_volume_ratio)
    seed_a = ignition & prepared_a
    seed_base = ignition & prepared_base & (f.close > high4)
    seed_pit = (
        ignition & pit & (f.close > platform_low + 0.1 * atr) & (f.volume_ratio >= 1.0)
    )
    seed_b = seed_base | seed_pit
    evidence_b = evidence_base.where(
        ~seed_pit, "先有横盘平台 / 下破形成坑 / 收回原支撑"
    )
    neckline_b = high4.where(~seed_pit, platform_low)
    a = track_leg(f, cfg, seed_a, low8, prepared_a, evidence_a, f.high.shift())
    b = track_leg(f, cfg, seed_b, low8, prepared_base | pit, evidence_b, neckline_b)
    f["ema_trigger"] = f.close.ewm(span=5, adjust=False, min_periods=5).mean()
    warm = f.close.rolling(20).count().eq(20) & f.daily_amount.notna() & f.atr.gt(0)
    liquid = f.daily_amount.ge(cfg.min_daily_amount)
    extension = f.extension_atr.le(cfg.max_extension_atr)
    for key, leg in [("a", a), ("b", b)]:
        for col in [c for c in leg.columns if c.startswith("startup_")]:
            f[f"{key}_{col}"] = leg[col]
        stop = np.minimum(leg.startup_low - 0.2 * f.atr, f.close - cfg.stop_atr * f.atr)
        risk = (f.close - stop) / f.close * 100
        f[f"{key}_stop"] = stop
        f[f"{key}_risk"] = risk
        f[f"{key}_safe"] = (
            warm
            & liquid
            & extension
            & (stop > 0)
            & risk.between(0, cfg.max_risk_pct, inclusive="right")
        )
        turn = (
            (f.close > f.ema_fast) & f.ema_fast.diff().gt(0)
            if key == "a"
            else (f.close > f.ema_trigger)
            & f.ema_trigger.diff().gt(0)
            & (f.close > leg.startup_neckline)
        )
        f[f"{key}_turn"] = turn
        smooth = leg.startup_efficiency.ge(cfg.startup_min_efficiency)
        stage = leg.startup_advancing & leg.startup_early
        f[f"{key}_eligible"] = (
            f[f"{key}_safe"] & turn & smooth & stage & leg.startup_count.between(2, 4)
        )
        f[f"{key}_watch"] = f[f"{key}_safe"] & stage & leg.startup_count.eq(1)
    current_trend = higher_context & f.close.ge(f.ema_slow - f.atr)
    f["a_eligible"] &= current_trend
    f["a_watch"] &= current_trend
    # A gets priority only at the SAME confirmation level; it cannot hide B.
    use_a = f.a_eligible | (~f.b_eligible & (f.a_watch | (~f.b_watch & context_a)))
    for col in [c for c in a.columns if c.startswith("startup_")]:
        f[col] = a[col].where(use_a, b[col])
    f["pattern"] = pd.Series("base_rebound", index=f.index).where(
        ~use_a, "trend_pullback"
    )
    f["pattern_label"] = f.pattern.map(LABELS)
    f["stop_price"] = f.a_stop.where(use_a, f.b_stop)
    f["risk_pct"] = f.a_risk.where(use_a, f.b_risk)
    f["eligible"] = f.a_eligible | f.b_eligible
    f["first_watch"] = f.a_watch | f.b_watch
    f["setup_context"] = ((a.startup_prepared & current_trend) | context_a).where(
        use_a, b.startup_prepared | prepared_base | pit
    )
    active = f.startup_count.gt(0) | f.setup_context
    f["pattern"] = f.pattern.where(active, "")
    f["pattern_label"] = f.pattern.map(LABELS).fillna("等待形态")
    f["timeframe"] = timeframe
    return f


def setup_result(f, cfg, timeframe):
    r = f.iloc[-1]
    key = "a" if r.pattern == "trend_pullback" else "b"
    checks = [
        (
            "context",
            "原升势与回调" if key == "a" else "阴跌/砸坑背景",
            bool(r.setup_context),
            "原有升势保持、回撤受控"
            if key == "a"
            else "允许日线仍偏弱；横盘阴跌修复，或平台下破后收回支撑",
        ),
        (
            "structure",
            "本段结构证据",
            bool(r.startup_prepared),
            r.startup_evidence or "尚无有效结构转强信号",
        ),
        (
            "liquidity",
            "流动性",
            bool(r.daily_amount >= cfg.min_daily_amount),
            f"20日日均成交额≥{cfg.min_daily_amount / 1e8:g}亿",
        ),
        (
            "turn",
            "推进转强",
            bool(r[f"{key}_turn"]),
            "重上EMA20且快线转升"
            if key == "a"
            else "站上EMA5与本段收复位；不强迫日线多头",
        ),
        (
            "smooth",
            "本段推进",
            bool(r.startup_efficiency >= cfg.startup_min_efficiency),
            f"本段效率≥{cfg.startup_min_efficiency:g}",
        ),
        (
            "extension",
            "不过度追高",
            bool(r.extension_atr <= cfg.max_extension_atr),
            f"高于EMA20的偏离≤{cfg.max_extension_atr:g}ATR",
        ),
        (
            "risk",
            "风险距离",
            bool(r.stop_price > 0 and 0 < r.risk_pct <= cfg.max_risk_pct),
            f"结构低点外、至少{cfg.stop_atr:g}ATR，距离≤{cfg.max_risk_pct:g}%",
        ),
        (
            "trigger",
            "第2—4推进确认",
            bool(r.startup_early and r.startup_advancing and 2 <= r.startup_count <= 4),
            "首个有效转强信号观察；第2—4推进阳线确认",
        ),
    ]
    ready = pd.notna(r.daily_amount) and pd.notna(r.atr) and r.atr > 0
    state = (
        f"启动第{int(r.startup_count)}阳"
        if r.eligible
        else "首阳观察"
        if r.first_watch
        else "数据不足"
        if not ready
        else "启动已过"
        if r.startup_count > 4
        else "趋势观察"
        if r.startup_count > 0
        else "等待形态"
    )
    risk = float(r.risk_pct) if pd.notna(r.risk_pct) else None
    score = 0.0
    if risk is not None:
        timing = max(0, (5 - int(r.startup_count)) / 3) if r.startup_count >= 2 else 0
        score = round(
            40 * sum(ok for _, _, ok, _ in checks) / len(checks)
            + 30 * float(r.startup_efficiency)
            + 20 * timing
            + 10 * max(0, 1 - risk / cfg.max_risk_pct),
            1,
        )

    def num(k):
        return round(float(r[k]), 6) if pd.notna(r[k]) and np.isfinite(r[k]) else None

    return {
        "state": state,
        "eligible": bool(r.eligible),
        "score": score,
        "pattern": r.pattern,
        "pattern_label": r.pattern_label,
        "timeframe": timeframe,
        "reason": "全部条件通过"
        if r.eligible
        else "有效转强首阳，等待推进确认"
        if r.first_watch
        else " / ".join(label for _, label, ok, _ in checks if not ok),
        "checks": [
            {"key": k, "label": label, "passed": ok, "rule": rule}
            for k, label, ok, rule in checks
        ],
        "metrics": {
            k: num(k)
            for k in [
                "close",
                "ema_fast",
                "ema_slow",
                "atr",
                "adx",
                "efficiency",
                "volume_ratio",
                "extension_atr",
                "daily_amount",
            ]
        },
        "stop_price": num("stop_price"),
        "risk_pct": risk,
        "bar_end": r.bar_end.isoformat(),
        "startup": {
            "start": r.startup_start.isoformat() if pd.notna(r.startup_start) else None,
            "bullish_count": int(r.startup_count),
            "age_bars": int(r.startup_age),
            "efficiency": round(float(r.startup_efficiency), 4),
            "early": bool(r.startup_early),
            "prepared": bool(r.startup_prepared),
            "evidence": r.startup_evidence,
            "neckline": num("startup_neckline"),
        },
    }
