"""Daily-close exits for a 60F reversal followed by a native daily entry.

All decisions become effective next bar. There is no assertion that a low
position guarantees safety or that a stop guarantees a fill at that price.
"""

import math


def decide(position, close, atr, held, cfg, cost_floor):
    entry, risk = position["entry_adjusted"], position["initial_risk"]
    peak_r = (position["peak"] - entry) / risk
    close_r = (close - entry) / risk
    stop = position["stop"]
    # A rebound that cannot advance should not consume a full forty-day hold.
    if (
        held >= cfg.rebound_patience
        and peak_r < cfg.rebound_min_progress_r
        and close < entry
    ):
        return stop, "rebound_failed"
    if math.isfinite(atr) and atr > 0:
        candidate = stop
        # A wick alone does not activate the cost floor: require a +1R close.
        if close_r >= cfg.protect_after_r:
            candidate = max(candidate, cost_floor)
        if peak_r >= cfg.trail_after_r:
            candidate = max(candidate, position["peak"] - cfg.trail_atr * atr)
        stop = max(stop, min(candidate, close - 0.1 * atr))
    # Early bases often sit below daily M21. Use that line to end a demonstrated
    # rebound, rather than forcing every initial base to become a trend at once.
    if (
        cfg.trend_exit
        and peak_r >= cfg.protect_after_r
        and held >= cfg.min_trend_holding_bars
        and position["failure_count"] >= cfg.failure_bars
    ):
        return stop, "trend_break_confirmed"
    if held >= cfg.max_holding_bars:
        return stop, "holding_timeout"
    return stop, None
