"""Immutable execution assumptions; legacy defaults remain reproducible."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000
    max_positions: int = 10
    position_weight: float = 0.1
    commission_rate: float = 0.0003
    minimum_commission: float = 5
    stamp_tax: float = 0.0005
    transfer_fee: float = 0.00001
    slippage: float = 0.0005
    participation: float = 0.01
    take_profit_r: float = 2
    max_holding_bars: int = 20
    max_entry_risk_pct: float = 8
    trend_exit: bool = True
    exit_policy: str = "legacy"
    risk_budget_pct: float = 0
    protect_after_r: float = 1.2
    trail_atr: float = 2.5
    failure_bars: int = 2
    min_trend_holding_bars: int = 4
    rebound_patience: int = 5
    rebound_min_progress_r: float = 0.5
    trail_after_r: float = 2
    # After-work mode: thresholds are checked at close, exits queue next open.
    # Intraday extremes never imply a discretionary fill in this mode.
    close_only: bool = False

    def __post_init__(self):
        if type(self.close_only) is not bool:
            raise ValueError("close_only必须为布尔值")
        numbers = [
            self.initial_cash,
            self.position_weight,
            self.commission_rate,
            self.minimum_commission,
            self.stamp_tax,
            self.transfer_fee,
            self.slippage,
            self.participation,
            self.take_profit_r,
            self.max_entry_risk_pct,
            self.risk_budget_pct,
            self.protect_after_r,
            self.trail_atr,
            self.rebound_min_progress_r,
            self.trail_after_r,
        ]
        if any(not math.isfinite(x) or x < 0 for x in numbers):
            raise ValueError("回测参数必须为有限非负数")
        if (
            self.initial_cash <= 0
            or not 0 < self.position_weight <= 1
            or not 0 < self.participation <= 1
        ):
            raise ValueError("初始资金/仓位/参与率不合法")
        if (
            type(self.max_positions) is not int
            or type(self.max_holding_bars) is not int
            or not 1 <= self.max_positions <= 100
            or self.max_holding_bars < 1
            or self.take_profit_r <= 0
            or self.max_entry_risk_pct <= 0
            or self.slippage >= 0.1
        ):
            raise ValueError("持仓/退出参数不合法")
        if self.exit_policy not in ("legacy", "protected", "reversal", "structure"):
            raise ValueError("未知退出策略")
        if (
            not 0 <= self.risk_budget_pct <= 5
            or self.protect_after_r <= 0
            or self.trail_atr <= 0
        ):
            raise ValueError("风险预算/跟踪参数不合法")
        if any(
            type(x) is not int or x < 1
            for x in [
                self.failure_bars,
                self.min_trend_holding_bars,
                self.rebound_patience,
            ]
        ):
            raise ValueError("确认/保护根数不合法")
        if self.rebound_min_progress_r <= 0 or (
            self.exit_policy == "reversal" and self.trail_after_r < self.protect_after_r
        ):
            raise ValueError("反转退出的推进/跟踪阈值不合法")

    def fee(self, notional, selling=False):
        return max(
            self.minimum_commission, notional * self.commission_rate
        ) + notional * (self.transfer_fee + (self.stamp_tax if selling else 0))


@dataclass(frozen=True)
class StructureExitConfig(BacktestConfig):
    exit_policy: str = "structure"
    protect_after_r: float = 1.5
    protect_after_pct: float = 3
    support_buffer_atr: float = 0.25
    max_holding_bars: int = 40
    close_only: bool = True

    def __post_init__(self):
        super().__post_init__()
        if self.exit_policy != "structure" or not self.close_only:
            raise ValueError("结构持有退出仅支持盘后检查、次日执行")
        if any(
            not math.isfinite(v) or v <= 0
            for v in (self.protect_after_pct, self.support_buffer_atr)
        ):
            raise ValueError("结构保护阈值不合法")
