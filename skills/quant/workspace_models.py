"""Validated, executable strategy drafts for the personal research workspace."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from skills.quant.rule_models import RuleStrategy


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, str_strip_whitespace=True
    )


Note = Annotated[str, Field(min_length=1, max_length=2000)]


class EntryRules(Model):
    family: Literal["both", "base_rebound", "trend_pullback"] = "both"
    min_body_atr: float = Field(0.2, gt=0, le=3)
    min_volume_ratio: float = Field(0.8, gt=0, le=5)
    divergence_improvement_atr: float = Field(0.04, gt=0, le=1)
    pullback_depth_atr: float = Field(3, gt=0, le=10)
    stop_buffer_atr: float = Field(0.35, gt=0, le=3)
    min_risk_pct: float = Field(0.8, gt=0, le=4)
    max_risk_pct: float = Field(4, gt=0, le=5)
    min_daily_amount: float = Field(30_000_000, gt=0, le=10_000_000_000)
    cooldown_bars: int = Field(12, ge=1, le=120, strict=True)

    @model_validator(mode="after")
    def ordered_risk(self):
        if self.min_risk_pct >= self.max_risk_pct:
            raise ValueError("最小风险距离必须小于最大风险距离")
        return self


class ExecutionRules(Model):
    initial_cash: float = Field(1_000_000, ge=10_000, le=100_000_000)
    max_positions: int = Field(10, ge=1, le=30, strict=True)
    position_weight: float = Field(0.1, gt=0, le=1)
    risk_budget_pct: float = Field(0.35, gt=0, le=1)
    max_holding_bars: int = Field(
        20,
        ge=1,
        le=120,
        strict=True,
        description="持有期按交易日计算，20代表20个交易日，绝不能乘4换算为60F根数",
    )
    exit_policy: Literal["legacy", "protected", "reversal"] = "protected"
    take_profit_r: float = Field(3, gt=0, le=20)
    protect_after_r: float = Field(1.2, gt=0, le=10)
    trail_atr: float = Field(2.5, gt=0, le=10)
    failure_bars: int = Field(2, ge=1, le=10, strict=True)
    trend_exit: bool = Field(True, strict=True)
    entry_plan: Literal["single", "442", "532"] = "single"
    exit_plan: Literal["single", "442", "532"] = "single"
    profit_first_pct: float = Field(5, gt=0, le=50)
    profit_second_pct: float = Field(8, gt=0, le=100)
    commission_rate: float = Field(0.0003, ge=0, le=0.01)
    minimum_commission: float = Field(5, ge=0, le=100)
    stamp_tax: float = Field(0.0005, ge=0, le=0.01)
    transfer_fee: float = Field(0.00001, ge=0, le=0.01)
    slippage: float = Field(0.0005, ge=0, le=0.02)

    @model_validator(mode="after")
    def ordered_profits(self):
        if (
            self.exit_plan != "single"
            and self.profit_second_pct <= self.profit_first_pct
        ):
            raise ValueError("第二档止盈必须高于第一档")
        if (
            self.entry_plan == self.exit_plan == "single"
            and self.exit_policy == "reversal"
            and self.protect_after_r > 2
        ):
            raise ValueError("分段离场保护启动不得晚于2R跟踪启动")
        return self


class StrategyDraft(Model):
    mode: Literal["template", "rules"] = "template"
    rules: RuleStrategy = Field(default_factory=RuleStrategy)
    name: str = Field("我的60F双机会策略", min_length=1, max_length=80)
    description: str = Field("", max_length=2000)
    interpretation_notes: list[Note] = Field(default_factory=list, max_length=30)
    unresolved_requirements: list[Note] = Field(default_factory=list, max_length=30)
    universe: Literal[
        "mainboard",
        "five_indices",
        "000300.SH",
        "000016.SH",
        "000905.SH",
        "000852.SH",
        "932000.CSI",
    ] = "mainboard"
    entry: EntryRules = Field(default_factory=EntryRules)
    execution: ExecutionRules = Field(default_factory=ExecutionRules)

    @model_validator(mode="after")
    def supported_execution(self):
        if self.mode == "rules" and (
            self.execution.entry_plan != "single"
            or self.execution.exit_plan != "single"
        ):
            raise ValueError("组合规则首版支持单次进出；442/532请使用已有60F模板模式")
        return self


class BacktestRequest(Model):
    draft_id: UUID
    request_id: UUID
    start: date
    end: date
    sample_limit: int = Field(200, ge=0, le=5000, strict=True)

    @model_validator(mode="after")
    def date_order(self):
        if not date(2026, 1, 1) <= self.start <= self.end <= date.today():
            raise ValueError("请在2026-01-01至今天之间选择有效区间")
        return self


class GenerateRequest(Model):
    text: str = Field(min_length=5, max_length=4000)
    base: StrategyDraft = Field(default_factory=StrategyDraft)
