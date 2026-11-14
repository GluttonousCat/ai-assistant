"""After-close structural carry; initial risk remains unchanged.

Protect only after demonstrated progress. Confirmed support must have formed
after entry before it can raise a stop. No future pivot or intraday fill is used.
"""

import math


def decide(position, close, atr, support, fresh_support, failed, held, cfg, cost_floor):
    stop = position["stop"]
    entry = position["entry_adjusted"]
    risk = position["initial_risk"]
    if (
        position["family"] == "base_rebound"
        and held >= cfg.rebound_patience
        and position["peak"] - entry < cfg.rebound_min_progress_r * risk
        and close < entry
    ):
        return stop, "rebound_failed"
    progress = close - entry
    if (
        progress >= cfg.protect_after_r * risk
        and progress / entry * 100 >= cfg.protect_after_pct
        and math.isfinite(atr)
        and atr > 0
    ):
        candidate = cost_floor
        if fresh_support and math.isfinite(support):
            candidate = max(candidate, support - cfg.support_buffer_atr * atr)
        stop = max(stop, min(candidate, close - 0.1 * atr))
    if failed and held >= cfg.min_trend_holding_bars:
        return stop, "structure_failed"
    if held >= cfg.max_holding_bars:
        return stop, "holding_timeout"
    return stop, None
