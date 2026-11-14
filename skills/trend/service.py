"""Dashboard composition: cached scan plus one stock's causal chart indicators."""

import json
import pandas as pd

from tools.market.trend_data import (
    adjusted_bars,
    adjusted_daily_bars,
    adjust_prices,
    load_dashboard,
)
from skills.trend.strategy import TrendConfig, evaluated_bars


def dashboard(run_id=None, include_rows=True):
    snapshot = load_dashboard(run_id, include_rows)
    if snapshot and run_id is None:
        from skills.trend.mtf import DUAL_VERSION

        snapshot = {**snapshot, "latest_rules_version": DUAL_VERSION}
    return snapshot


def stock_detail(
    code: str, start: str, end: str, config: dict | None = None, timeframe: str = "60F"
):
    if (config or {}).get("selection_mode") == "mtf":
        return _mtf_detail(code, start, end, config, timeframe)
    frame = (
        adjusted_daily_bars(code, start, end)
        if timeframe == "1D"
        else adjusted_bars(code, start, end)
    )
    frame, issue = adjust_prices(frame)
    if frame.empty:
        return {"data": [], "issue": issue, "price_basis": "raw", "code": code}
    if issue:
        for c in ("open", "high", "low", "close", "volume", "amount"):
            frame[c] = pd.to_numeric(frame[c], errors="coerce")
    settings = dict(config or {})
    # v1 snapshots have no mode field; retain their mature-trend interpretation.
    if config is not None:
        settings.setdefault("selection_mode", "mature")
        settings.setdefault("require_precursor", False)
        settings.setdefault("track_swing_context", False)
    data = evaluated_bars(frame, TrendConfig(**settings), timeframe)
    # Display averages are independent of the snapshot's strategy EMA settings.
    # Compute before tail(1000) so the left edge keeps its full warm-up history.
    for window in (8, 13, 21):
        data[f"ma{window}"] = data.close.rolling(window, min_periods=window).mean()
    if issue:
        data["eligible"] = False
        data["first_watch"] = False
        if "resume_watch" in data:
            data["resume_watch"] = False
            data["swing_ignition"] = False
    cols = [
        "bar_end",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "ema_fast",
        "ema_slow",
        "ma13",
        "ma21",
        "ma8",
        "atr",
        "adx",
        "efficiency",
        "startup_start",
        "startup_count",
        "startup_age",
        "startup_efficiency",
        "eligible",
        "first_watch",
        "startup_evidence",
        "startup_prepared",
        "startup_neckline",
    ]
    cols += [c for c in ["pattern", "pattern_label"] if c in data.columns]
    cols += [
        c
        for c in [
            "resume_watch",
            "setup_phase",
            "swing_start",
            "swing_ignition",
            "background_swing_start",
        ]
        if c in data.columns
    ]
    clean = (
        data[cols]
        .tail(1000)
        .astype(object)
        .where(pd.notna(data[cols].tail(1000)), None)
    )
    return {
        "code": code,
        "data": json.loads(clean.to_json(orient="records", date_format="iso")),
        "issue": issue,
        "price_basis": "raw" if issue else "forward_asof",
        "as_of": end,
        "timeframe": timeframe,
        "time_zone": "Asia/Shanghai",
        "volume_unit": "share",
        "amount_unit": "CNY",
        "chart_averages": {"type": "sma", "windows": [8, 13, 21]},
    }


def _mtf_detail(code, start, end, config, timeframe):
    from skills.trend.mtf import MtfConfig, evaluate, features

    settings = dict(config)
    settings.setdefault(
        "slope_epsilon_atr", 0.0
    )  # Keep the superseded v5 chart reproducible.
    cfg = MtfConfig(**settings)
    minute, mi = adjust_prices(adjusted_bars(code, start, end))
    daily, di = adjust_prices(adjusted_daily_bars(code, start, end))
    chosen = minute if timeframe == "60F" else daily
    price_issue = mi if timeframe == "60F" else di
    if chosen.empty:
        return {"code": code, "data": [], "issue": price_issue, "price_basis": "raw"}
    issue = mi or di
    if issue:
        data = features(chosen, cfg)
        data["eligible"] = False
    else:
        m, d = evaluate(minute, daily, cfg)
        data = m if timeframe == "60F" else d
        if timeframe == "60F":
            data["eligible"] = False
        elif "reversal_ignition" in data:
            # Observation origins belong on 60F; daily arrows remain entries.
            data["reversal_ignition"] = False
    data["first_watch"] = False
    data["resume_watch"] = False
    data["swing_ignition"] = False
    cols = [
        c
        for c in [
            "bar_end",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            f"ma{cfg.fast}",
            f"ma{cfg.mid}",
            f"ma{cfg.slow}",
            "atr",
            "eligible",
            "first_watch",
            "resume_watch",
            "swing_ignition",
            "pattern",
            "pattern_label",
            "trend_ready",
            "trend_state",
            "state",
            "context_end",
            "stop_price",
            "reversal_start",
            "reversal_age",
            "reversal_bulls",
            "reversal_ignition",
            "reversal_fresh",
        ]
        if c in data
    ]
    clean = (
        data[cols]
        .tail(1000)
        .astype(object)
        .where(pd.notna(data[cols].tail(1000)), None)
    )
    return {
        "code": code,
        "data": json.loads(clean.to_json(orient="records", date_format="iso")),
        "issue": issue,
        "price_basis": "raw" if price_issue else "forward_asof",
        "as_of": end,
        "timeframe": timeframe,
        "trend_timeframe": "60F",
        "entry_timeframe": "1D",
        "time_zone": "Asia/Shanghai",
        "volume_unit": "share",
        "amount_unit": "CNY",
        "chart_averages": {"type": "sma", "windows": [cfg.fast, cfg.mid, cfg.slow]},
        "version_notice": "该历史v5数值版本存在浮点走平误判，已由v5.1替代；仅供核查"
        if "slope_epsilon_atr" not in config
        else None,
    }
