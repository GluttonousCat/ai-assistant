from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from skills.quant.rule_models import Constant, Indicator, Condition, Group, RuleStrategy
from skills.quant.rule_indicators import Evaluator, indicator
from skills.quant.rule_prepare import prepare_rules
from skills.quant.workspace_models import StrategyDraft
from skills.quant.workspace_runner import execution_config
from skills.quant.engine import simulate
from skills.quant.models import RuleExecutionConfig
from skills.quant.prepare import MarketSeries


def frame(close, start="2026-01-05", daily=False):
    times = (
        pd.date_range(start, periods=len(close), freq="D") + pd.Timedelta(hours=15)
        if daily
        else pd.date_range(start + " 10:30", periods=len(close), freq="h")
    )
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        dict(
            bar_end=times,
            open=close - 0.1,
            high=close + 0.2,
            low=close - 0.2,
            close=close,
            raw_close=close,
            volume=100.0,
            amount=1e8,
        )
    )


def condition(left="close", op="gt", right=0, **kwargs):
    return Condition(
        left=Indicator(metric=left, **kwargs), operator=op, right=Constant(value=right)
    )


@pytest.mark.parametrize(
    "value",
    [
        dict(metric="python"),
        dict(metric="ma"),
        dict(metric="close", period=13),
        dict(metric="close", lag=-1),
        dict(metric="ma", period=1),
        dict(metric="rsi", period=True),
        dict(metric="close", multiplier=float("nan")),
        dict(metric="close", timeframe="5F"),
    ],
)
def test_rule_language_rejects_unknown_code_future_references_and_invalid_periods(
    value,
):
    with pytest.raises(ValidationError):
        Indicator.model_validate(value)


def test_nested_logic_is_bounded_and_custom_tranches_are_not_silently_approximated():
    node = Group(children=[condition()])
    for _ in range(4):
        node = Group(children=[node])
    with pytest.raises(ValidationError, match="嵌套"):
        RuleStrategy(entry=node)
    with pytest.raises(ValidationError):
        Group(children=[])
    with pytest.raises(ValidationError, match="单次进出"):
        StrategyDraft(mode="rules", execution={"entry_plan": "532"})


def test_raw_and_adjusted_price_units_cannot_be_mixed():
    with pytest.raises(ValidationError, match="不复权收盘价"):
        Condition(
            left=Indicator(metric="raw_close"), right=Indicator(metric="ma", period=13)
        )
    assert Condition(left=Indicator(metric="raw_close"), right=Constant(value=20))


def test_one_ulp_is_not_a_crossover_after_price_rebasing():
    f = frame([100.0] * 40)
    f.loc[30, "close"] = np.nextafter(100.0, np.inf)
    rule = Condition(
        left=Indicator(metric="close"),
        operator="cross_up",
        right=Indicator(metric="ma", period=13),
    )
    first = Evaluator(f, pd.DataFrame(), pd.DatetimeIndex(f.bar_end)).evaluate(rule)
    rebased = f.copy()
    for key in ("open", "high", "low", "close"):
        rebased[key] *= 1.313131313131
    second = Evaluator(rebased, pd.DataFrame(), pd.DatetimeIndex(f.bar_end)).evaluate(
        rule
    )
    assert not first.any()
    pd.testing.assert_series_equal(first, second)


def test_native_indicator_values_and_prior_extremes_exclude_current_bar():
    f = frame([10, 11, 12, 13, 50])
    np.testing.assert_allclose(
        indicator(f, Indicator(metric="ma", period=2)).to_numpy()[1:],
        [10.5, 11.5, 12.5, 31.5],
    )
    assert indicator(f, Indicator(metric="prior_high", period=2)).iloc[-1] == 13.2
    assert indicator(f, Indicator(metric="volume_ratio", period=2)).iloc[-1] == 1
    assert indicator(f, Indicator(metric="rsi", period=2)).iloc[-1] == 100
    assert indicator(frame([10] * 10), Indicator(metric="rsi", period=2)).iloc[-1] == 50
    assert indicator(f, Indicator(metric="return_pct", period=2)).iloc[
        -1
    ] == pytest.approx((50 / 12 - 1) * 100)


@pytest.mark.parametrize(
    "metric,period",
    [
        ("ma", 3),
        ("ema", 3),
        ("rsi", 3),
        ("atr", 3),
        ("volume_ratio", 3),
        ("prior_high", 3),
        ("prior_low", 3),
        ("return_pct", 3),
        ("macd_dif", None),
        ("macd_dea", None),
        ("macd_hist", None),
    ],
)
def test_every_indicator_is_prefix_invariant(metric, period):
    f = frame(10 + np.sin(np.arange(80) / 4) + np.arange(80) * 0.1)
    ref = Indicator(metric=metric, period=period)
    pd.testing.assert_series_equal(
        indicator(f.iloc[:50], ref), indicator(f, ref).iloc[:50]
    )


def test_daily_context_never_reads_todays_future_close_and_daily_cross_does_not_repeat():
    d = frame([9, 11, 12], daily=True)
    clock = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-01-06 10:30"),
            pd.Timestamp("2026-01-06 15:00"),
            pd.Timestamp("2026-01-07 10:30"),
            pd.Timestamp("2026-01-07 15:00"),
        ]
    )
    m = frame([1, 2, 3, 4])
    m["bar_end"] = clock
    evaluator = Evaluator(m, d, clock)
    daily = Indicator(metric="close", timeframe="1D")
    assert evaluator.operand(daily).tolist() == [9, 11, 11, 12]
    rule = Condition(left=daily, operator="cross_up", right=Constant(value=10))
    assert evaluator.evaluate(rule).tolist() == [False, True, False, False]


def test_and_or_crossover_missing_values_and_lags():
    f = frame([9, 11, 12, 8, 11])
    ev = Evaluator(f, pd.DataFrame(), pd.DatetimeIndex(f.bar_end))
    cross = condition(op="cross_up", right=10)
    assert ev.evaluate(cross).tolist() == [False, True, False, False, True]
    group = Group(logic="all", children=[cross, condition(right=11.5)])
    assert not ev.evaluate(group).any()
    assert (
        ev.evaluate(Group(logic="any", children=[cross, condition(right=11.5)])).sum()
        == 3
    )
    ref = Indicator(metric="close", lag=2, multiplier=2)
    assert np.isnan(ev.operand(ref).iloc[0]) and ev.operand(ref).iloc[2] == 18
    unknown = Condition(left=Indicator(metric="ma", period=10), right=Constant(value=0))
    assert not ev.evaluate(unknown).any()


def stock_fixture():
    times = pd.bdate_range("2026-01-05", periods=6) + pd.Timedelta(hours=15)
    data = {
        k: np.full(6, value, dtype=float)
        for k, value in dict(
            open=10,
            high=10.4,
            low=9.6,
            close=10,
            volume=1e8,
            amount=1e9,
            scale=1,
            up_limit=20,
            down_limit=1,
            trail_a=100,
            trail_b=100,
            atr=1,
        ).items()
    }
    for key in ("open", "high", "low", "close"):
        data["adjusted_" + key] = data[key].copy()
    data["rule_exit"] = np.zeros(6, dtype=bool)
    signal = dict(
        i=0,
        family="custom_rule",
        preferred=True,
        leg=times[0].isoformat(),
        count=1,
        stop=1.0,
        score=100.0,
    )
    return MarketSeries("600000.SH", "样本", times[0], times, data, [signal]), times


def test_fixed_stop_uses_actual_fill_not_signal_close_and_rule_exit_is_next_day():
    stock, clock = stock_fixture()
    stock.data["open"][1] = stock.data["adjusted_open"][1] = 11
    stock.data["high"][1] = stock.data["adjusted_high"][1] = 11.1
    stock.data["rule_exit"][1] = True
    cfg = RuleExecutionConfig(
        slippage=0,
        position_weight=0.5,
        max_entry_risk_pct=5,
        stop_loss_pct=3,
        take_profit_pct=None,
    )
    result = simulate([stock], clock, "custom_rules_v1", "1D", cfg=cfg)
    trade = result["trades"][0]
    assert trade["initial_stop"] == pytest.approx(11 * 0.97)
    assert (
        trade["reason"] == "stop_loss"
    )  # entry-day close already below its stop; risk exit wins
    assert trade["entry_time"].startswith("2026-01-06") and trade[
        "exit_time"
    ].startswith("2026-01-07")
    stock, clock = stock_fixture()
    stock.data["rule_exit"][2] = True
    result = simulate([stock], clock, "custom_rules_v1", "1D", cfg=cfg)
    trade = result["trades"][0]
    assert trade["reason"] == "rule_exit" and trade["exit_time"].startswith(
        "2026-01-08"
    )


def test_no_hidden_ma_exit_or_r_target_and_fixed_percent_profit():
    stock, clock = stock_fixture()
    stock.data["close"][2:] = stock.data["adjusted_close"][2:] = 11
    stock.data["high"][2:] = stock.data["adjusted_high"][2:] = 11.1
    cfg = RuleExecutionConfig(slippage=0, max_entry_risk_pct=5, take_profit_pct=None)
    result = simulate([stock], clock, "custom_rules_v1", "1D", cfg=cfg)
    assert not result["trades"] and len(result["open_positions"]) == 1
    stock.row_at = None
    result = simulate(
        [stock], clock, "custom_rules_v1", "1D", cfg=replace(cfg, take_profit_pct=8)
    )
    assert result["trades"][0]["reason"] == "take_profit"
    draft = StrategyDraft(
        mode="rules", execution={"trend_exit": True, "exit_policy": "protected"}
    )
    assert (
        execution_config(draft).trend_exit is False
        and execution_config(draft).exit_policy == "legacy"
    )


def test_compilation_uses_daily_only_without_minutes_and_preserves_raw_arrays():
    stock, _ = stock_fixture()
    original = {k: v.copy() for k, v in stock.data.items()}
    rules = RuleStrategy(
        entry=Group(children=[condition(right=9, timeframe="1D")]),
        min_daily_amount=0,
        trigger="level",
        cooldown_bars=0,
    )
    prepared, issue = prepare_rules(pd.DataFrame(), stock, rules)
    assert issue is None and len(prepared.signals) == 6
    for key in original:
        np.testing.assert_array_equal(stock.data[key], original[key])
    prefix = MarketSeries(
        stock.code,
        stock.name,
        stock.observed_at,
        stock.times[:3],
        {k: v[:3].copy() for k, v in stock.data.items()},
        [],
    )
    shorter, _ = prepare_rules(pd.DataFrame(), prefix, rules)
    assert shorter.signals == prepared.signals[:3]


def test_minute_rules_are_factor_consistent_and_incomplete_days_never_form_orders():
    stock, _ = stock_fixture()
    stock.data["scale"][3:] = 2
    for key in ("open", "high", "low", "close"):
        stock.data[key][3:] /= 2
    slots = [(10, 30), (11, 30), (14, 0), (15, 0)]
    rows = []
    for i, end in enumerate(stock.times):
        raw = stock.data["close"][i]
        for hour, minute in slots:
            rows.append(
                dict(
                    bar_end=end.normalize() + pd.Timedelta(hours=hour, minutes=minute),
                    open=raw,
                    close=raw,
                    high=raw + 0.1,
                    low=raw - 0.1,
                    volume=100.0,
                )
            )
    minute = pd.DataFrame(rows)
    original = minute.copy(deep=True)
    rules = RuleStrategy(
        entry=Group(children=[condition(right=9)]),
        min_daily_amount=0,
        trigger="level",
        cooldown_bars=0,
    )
    prepared, issue = prepare_rules(minute, stock, rules)
    assert issue is None and len(prepared.signals) == 6
    pd.testing.assert_frame_equal(minute, original)
    shorter, issue = prepare_rules(minute.iloc[:-1], stock, rules)
    assert issue is None and len(shorter.signals) == 5
    raw_rules = RuleStrategy(
        entry=Group(children=[condition(left="raw_close", op="lt", right=6)]),
        min_daily_amount=0,
        trigger="level",
        cooldown_bars=0,
    )
    prepared, issue = prepare_rules(minute, stock, raw_rules)
    assert issue is None and [s["i"] for s in prepared.signals] == [3, 4, 5]


def test_workspace_runner_dispatches_custom_rules_instead_of_template(monkeypatch):
    from tests.skills.quant.test_workspace import _inputs
    from skills.quant import workspace_runner

    task, _, _ = _inputs(monkeypatch)
    task["spec"].update(
        mode="rules",
        rules=RuleStrategy(
            entry=Group(children=[condition(right=0)]),
            min_daily_amount=0,
            trigger="level",
            cooldown_bars=0,
        ).model_dump(),
    )

    def forbidden(*args):
        pytest.fail("custom rules must not run v8 template")

    monkeypatch.setattr(workspace_runner, "prepare_swing", forbidden)
    result = workspace_runner.run_task(task)
    assert result["strategy_id"] == "custom_rules_v1"
    assert result["execution_version"] == "composable-rule-fixed-percent-v1"
    assert result["config"]["stop_loss_pct"] == 3
    assert result["config"]["trend_exit"] is False
