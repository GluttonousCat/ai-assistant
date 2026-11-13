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
    return load_dashboard(run_id, include_rows)


def stock_detail(
    code: str, start: str, end: str, config: dict | None = None, timeframe: str = "60F"
):
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
    for window in (13, 21):
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
        "chart_averages": {"type": "sma", "windows": [13, 21]},
    }
