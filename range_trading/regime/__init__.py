"""
日K Regime 层入口
"""
from range_trading.regime.daily_regime import run_daily_regime
from range_trading.regime.state_machine import (
    DailyRangeStateMachine,
    RangeState,
    Regime,
    TREND_STATES,
    build_range_state,
)
from range_trading.regime.trend_regime import run_trend_regime
from range_trading.regime.trend_state_machine import (
    TrendState,
    TrendStateMachine,
    build_trend_state,
    detect_event,
)

__all__ = [
    "run_daily_regime",
    "DailyRangeStateMachine",
    "RangeState",
    "Regime",
    "TREND_STATES",
    "build_range_state",
    "run_trend_regime",
    "TrendState",
    "TrendStateMachine",
    "build_trend_state",
    "detect_event",
]
