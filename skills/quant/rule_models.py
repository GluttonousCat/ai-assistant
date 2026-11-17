"""A bounded declarative rule language; no executable expressions."""

from typing import Annotated, Literal, Union, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

Metric = Literal[
    "open",
    "high",
    "low",
    "close",
    "raw_close",
    "volume",
    "ma",
    "ema",
    "rsi",
    "atr",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "volume_ratio",
    "prior_high",
    "prior_low",
    "return_pct",
]
METRICS = get_args(Metric)
PERIOD_METRICS = {
    "ma",
    "ema",
    "rsi",
    "atr",
    "volume_ratio",
    "prior_high",
    "prior_low",
    "return_pct",
}


class RuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Indicator(RuleModel):
    type: Literal["indicator"] = "indicator"
    metric: Metric
    timeframe: Literal["60F", "1D"] = "60F"
    period: int | None = Field(None, ge=2, le=250, strict=True)
    lag: int = Field(0, ge=0, le=250, strict=True)
    multiplier: float = Field(1, gt=0, le=100)

    @model_validator(mode="after")
    def meaningful_period(self):
        if (self.metric in PERIOD_METRICS) != (self.period is not None):
            raise ValueError(
                "均线/RSI/ATR/量比/前高低/涨幅必须指定周期，其他指标不接受周期"
            )
        return self


class Constant(RuleModel):
    type: Literal["constant"] = "constant"
    value: float = Field(0, ge=-1e12, le=1e12)


Operand = Annotated[Union[Indicator, Constant], Field(discriminator="type")]


class Condition(RuleModel):
    type: Literal["condition"] = "condition"
    left: Indicator
    operator: Literal["gt", "ge", "lt", "le", "cross_up", "cross_down"] = "gt"
    right: Operand

    @model_validator(mode="after")
    def consistent_price_units(self):
        if isinstance(self.right, Indicator) and (
            (self.left.metric == "raw_close") != (self.right.metric == "raw_close")
        ):
            raise ValueError("不复权收盘价不能与复权指标混比；名义股价筛选请与数值比较")
        return self


class Group(RuleModel):
    type: Literal["group"] = "group"
    logic: Literal["all", "any"] = "all"
    children: list[
        Annotated[Union[Condition, "Group"], Field(discriminator="type")]
    ] = Field(min_length=1, max_length=12)


def references(node):
    if isinstance(node, Group):
        for child in node.children:
            yield from references(child)
    else:
        yield node.left
        if isinstance(node.right, Indicator):
            yield node.right


def _default_entry():
    return Group(
        children=[
            Condition(
                left=Indicator(metric="close"),
                operator="cross_up",
                right=Indicator(metric="ma", period=13),
            )
        ]
    )


class RuleStrategy(RuleModel):
    entry: Group = Field(default_factory=_default_entry)
    exit: Group | None = None
    sampling: Literal["at_close", "any_completed_60f"] = "at_close"
    trigger: Literal["edge", "level"] = "edge"
    stop_loss_pct: float = Field(3, gt=0, le=5)
    take_profit_pct: float | None = Field(8, gt=0, le=100)
    cooldown_bars: int = Field(
        12,
        ge=0,
        le=250,
        strict=True,
        description="冷却以输入的60F根数计；仅日K指标的策略以交易日计",
    )
    min_daily_amount: float = Field(30_000_000, ge=0, le=1e10)

    @model_validator(mode="after")
    def bounded_tree(self):
        def size(node, depth=0):
            if depth > 3:
                raise ValueError("条件组最多嵌套3层")
            return (
                1 + sum(size(c, depth + 1) for c in node.children)
                if isinstance(node, Group)
                else 1
            )

        if sum(size(n) for n in (self.entry, self.exit) if n is not None) > 40:
            raise ValueError("入场与离场合计最多40个条件/分组")
        return self

    def needs_minutes(self):
        return any(
            ref.timeframe == "60F"
            for node in (self.entry, self.exit)
            if node is not None
            for ref in references(node)
        )
