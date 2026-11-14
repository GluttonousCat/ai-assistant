"""Position plans and FIFO cost accounting for staged cash-equity campaigns.

Units share the existing factor-share approximation. Each acquisition retains
its own settlement date; partial sales do not become independent winning trades.
"""

from dataclasses import dataclass, field
import math

from skills.quant.models import BacktestConfig


@dataclass(frozen=True)
class TrancheConfig:
    base: BacktestConfig = field(
        default_factory=lambda: BacktestConfig(
            close_only=True,
            risk_budget_pct=0.35,
            max_entry_risk_pct=4,
            max_holding_bars=20,
        )
    )
    entry_weights: tuple = (0.4, 0.4, 0.2)
    exit_weights: tuple = (0.4, 0.4, 0.2)
    add_at_r: tuple = (0.5, 1.0)
    take_profit_pct: tuple = (5.0, 8.0)
    protect_at_pct: float = 5

    def __post_init__(self):
        if not self.base.close_only or self.base.risk_budget_pct <= 0:
            raise ValueError("分批模型使用盘后决策及正风险预算")
        for values in (self.entry_weights, self.exit_weights):
            if (
                not values
                or any(not math.isfinite(x) or x <= 0 for x in values)
                or not math.isclose(sum(values), 1.0, abs_tol=1e-10)
            ):
                raise ValueError("分批比例必须为正数且总和为1")
        for thresholds, n in [
            (self.add_at_r, len(self.entry_weights) - 1),
            (self.take_profit_pct, len(self.exit_weights) - 1),
        ]:
            if (
                len(thresholds) != n
                or any(not math.isfinite(x) or x <= 0 for x in thresholds)
                or any(b <= a for a, b in zip(thresholds, thresholds[1:]))
            ):
                raise ValueError("分批触发阈值数量/顺序不合法")
        if not math.isfinite(self.protect_at_pct) or self.protect_at_pct <= 0:
            raise ValueError("盈利保护阈值不合法")


@dataclass
class Lot:
    units: float
    cost: float
    day: object


@dataclass
class Inventory:
    lots: list = field(default_factory=list)
    bought_cost: float = 0
    received: float = 0

    @property
    def units(self):
        return sum(lot.units for lot in self.lots)

    @property
    def cost(self):
        return sum(lot.cost for lot in self.lots)

    @property
    def realized(self):
        return self.received - (self.bought_cost - self.cost)

    def available(self, day):
        return sum(lot.units for lot in self.lots if lot.day < day)

    def risk_at(self, stop):
        return sum(max(0, lot.cost - lot.units * stop) for lot in self.lots)

    def buy(self, units, cost, day):
        if not all(math.isfinite(x) and x > 0 for x in (units, cost)):
            raise ValueError("新增份额与成本不合法")
        self.lots.append(Lot(units, cost, day))
        self.bought_cost += cost

    def sell(self, units, net_received, day):
        if (
            not math.isfinite(units)
            or units <= 0
            or not math.isfinite(net_received)
            or net_received < 0
            or units > self.available(day) + 1e-8
        ):
            raise ValueError("卖出超过T+1可用份额或金额不合法")
        remaining = units
        removed = 0
        for lot in self.lots:
            if lot.day >= day or remaining <= 1e-10:
                continue
            take = min(remaining, lot.units)
            cost = lot.cost * take / lot.units
            lot.units -= take
            lot.cost -= cost
            removed += cost
            remaining -= take
        self.lots = [lot for lot in self.lots if lot.units > 1e-10]
        self.received += net_received
        return net_received - removed
