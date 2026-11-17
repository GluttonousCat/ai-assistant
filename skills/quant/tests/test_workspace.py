from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from skills.quant import workspace, workspace_runner as runner
from skills.quant.workspace_models import (
    StrategyDraft,
    BacktestRequest,
    GenerateRequest,
)
from storage.repositories import quant_workspace as store


@pytest.fixture
def local_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_root", lambda: tmp_path)
    return store


def request(draft_id):
    return dict(
        draft_id=draft_id,
        request_id=str(uuid4()),
        start="2026-01-01",
        end="2026-11-12",
        sample_limit=50,
    )


def test_versions_are_immutable_and_user_isolated(local_store):
    spec = StrategyDraft().model_dump()
    first = store.save_draft(1, spec)
    spec["entry"]["max_risk_pct"] = 3
    second = store.save_draft(1, spec)
    assert first["id"] != second["id"]
    rows = store.list_drafts(1)
    assert len(rows) == 2 and {r["spec"]["entry"]["max_risk_pct"] for r in rows} == {
        3,
        4,
    }
    assert store.list_drafts(2) == []
    with pytest.raises(LookupError):
        store.enqueue(2, request(first["id"]))
    with pytest.raises(ValueError):
        store.task(1, "../../outside")


def test_queue_idempotency_freeze_and_capacity(local_store):
    draft = store.save_draft(1, StrategyDraft().model_dump())
    payload = request(draft["id"])
    first = store.enqueue(1, payload)
    assert store.enqueue(1, payload) == first
    assert store.task(2, first["id"]) is None
    assert store.load_result(2, first["id"]) is None
    with pytest.raises(ValueError):
        store.enqueue(1, {**payload, "sample_limit": 200})
    for _ in range(2):
        store.enqueue(1, request(draft["id"]))
    with pytest.raises(store.QueueBusy):
        store.enqueue(1, request(draft["id"]))
    assert len(store.list_tasks(1)) == 3
    assert store.next_task()["id"] == first["id"]


def test_recovery_does_not_silently_replay_running_task(local_store):
    draft = store.save_draft(1, StrategyDraft().model_dump())
    task = store.enqueue(1, request(draft["id"]))
    store.update_task(task["id"], status="running")
    store.recover_tasks()
    assert store.task(1, task["id"])["status"] == "interrupted"
    store.save_result(
        task["id"], {"metrics": {"closed_trades": 3}, "trades": [1, 2, 3]}
    )
    store.update_task(task["id"], status="running")
    store.recover_tasks()
    assert store.task(1, task["id"])["metrics"] == {"closed_trades": 3}
    assert store.load_result(1, task["id"], 1, 1)["trades"] == [2]
    assert store.load_result(2, task["id"], 1, 1) is None


def test_unsupported_requirements_remain_blocked_after_saving_and_loading(local_store):
    draft = StrategyDraft(unresolved_requirements=["自定义RSI暂不支持"])
    saved = store.save_draft(1, draft.model_dump())
    with pytest.raises(ValueError, match="未支持"):
        store.enqueue(1, request(saved["id"]))


@pytest.mark.parametrize(
    "changes",
    [
        {"entry": {"max_risk_pct": 6}},
        {"entry": {"min_risk_pct": 4, "max_risk_pct": 3}},
        {"execution": {"risk_budget_pct": 9}},
        {"execution": {"slippage": float("nan")}},
        {"execution": {"max_positions": True}},
        {"execution": {"trend_exit": "false"}},
        {
            "execution": {
                "exit_plan": "532",
                "profit_first_pct": 10,
                "profit_second_pct": 8,
            }
        },
        {"entry": {"python": "print('never execute')"}},
        {"universe": "../../outside"},
    ],
)
def test_unsafe_or_unsupported_config_rejected(changes):
    with pytest.raises(ValidationError):
        StrategyDraft.model_validate(changes)


def test_date_and_integer_validation():
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate({**request(str(uuid4())), "end": "2099-01-01"})
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate({**request(str(uuid4())), "sample_limit": True})


def test_natural_language_only_compiles_a_valid_draft(monkeypatch):
    base = StrategyDraft()
    response = dict(
        draft=base.model_dump(),
        assumptions=["采用默认参数"],
        unsupported=["自定义RSI规则暂不支持"],
    )
    import json

    seen = []

    def invoke(prompt):
        seen.append(prompt)
        return "```json\n" + json.dumps(response, ensure_ascii=False) + "\n```"

    monkeypatch.setattr(
        workspace, "get_agent_llm", lambda **kwargs: SimpleNamespace(invoke=invoke)
    )
    result = workspace.generate(GenerateRequest(text="增加RSI低于30", base=base))
    assert result["unsupported"] == response["unsupported"]
    assert result["draft"]["unresolved_requirements"] == response["unsupported"]
    assert result["draft"]["interpretation_notes"] == response["assumptions"]
    assert "不能将要求的固定百分比硬止损当成结构风险距离" in seen[0][0]["content"]
    response["draft"]["execute"] = "unsafe"
    with pytest.raises(ValidationError):
        workspace.generate(GenerateRequest(text="增加RSI低于30"))


def _inputs(monkeypatch):
    from skills.trend.tests.test_swing import bars

    minute = bars()
    minute["ts_code"] = "600000.SH"
    minute["adj_factor"] = 1.0
    minute["up_limit"] = 200.0
    minute["down_limit"] = 1.0
    day = minute.bar_end.dt.normalize()
    daily = minute.groupby(day).agg(
        ts_code=("ts_code", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
        adj_factor=("adj_factor", "last"),
        up_limit=("up_limit", "last"),
        down_limit=("down_limit", "last"),
    )
    daily["bar_end"] = daily.index + pd.Timedelta(hours=15)
    daily = daily.reset_index(drop=True)
    days = [t.date() for t in daily.bar_end]
    monkeypatch.setattr(
        runner,
        "scan_cutoffs",
        lambda now: dict(
            bar_end=minute.bar_end.max(), daily_end=days[-1], factor_date=days[-1]
        ),
    )
    monkeypatch.setattr(
        runner, "input_dates", lambda *args: (days, set(days), set(days))
    )
    members = [
        dict(
            ts_code="600000.SH",
            name="样本",
            membership_date=date(2026, 1, 1),
            indices=["000300.SH"],
        )
    ]
    monkeypatch.setattr(runner, "scan_universe", lambda end: deepcopy(members))
    monkeypatch.setattr(
        runner,
        "observed_dates",
        lambda *args: {"600000.SH": pd.Timestamp("2026-11-12")},
    )
    monkeypatch.setattr(
        runner,
        "quotes",
        lambda codes, start, end, timeframe: (
            minute if timeframe == "60F" else daily
        ).copy(deep=True),
    )
    monkeypatch.setattr(
        runner,
        "benchmarks",
        lambda *args: [
            {"trade_date": day, "ts_code": code, "close": 100 + i}
            for i, day in enumerate(days)
            for code in ["000300.SH", "000852.SH"]
        ],
    )
    task = dict(
        id=str(uuid4()),
        spec=StrategyDraft().model_dump(),
        request={**request(str(uuid4())), "start": str(days[0]), "end": str(days[-1])},
    )
    return task, minute, daily


@pytest.mark.parametrize("plan", ["single", "442", "532"])
def test_runner_reuses_original_causal_engine_and_does_not_change_inputs(
    monkeypatch, plan
):
    task, minute, daily = _inputs(monkeypatch)
    task["spec"]["execution"].update(entry_plan=plan, exit_plan=plan)
    original = minute.copy(deep=True)
    prepared = []
    original_prepare = runner.prepare_swing

    def capture(*args):
        stock, issue = original_prepare(*args)
        prepared.append(stock)
        return stock, issue

    monkeypatch.setattr(runner, "prepare_swing", capture)
    result = runner.run_task(task)
    assert result["workspace_spec"] == task["spec"]
    assert result["diagnostics"]["eligible_for_default"] is False
    assert result["config"]["close_only"] is True
    assert result["config"]["max_entry_risk_pct"] == 4
    assert len(result["equity"]) == len(daily)
    pd.testing.assert_frame_equal(minute, original)
    assert prepared[0].signals == sorted(prepared[0].signals, key=lambda s: s["i"])
    assert result["equity"][0]["benchmarks"]["000300.SH"] == 1
    if plan != "single":
        assert result["config"]["exit_policy"] == "tranches"
        assert result["config"]["entry_weights"] == (
            (0.4, 0.4, 0.2) if plan == "442" else (0.5, 0.3, 0.2)
        )


def test_no_future_window_or_missing_limits_can_be_reported_as_zero_return(monkeypatch):
    task, _, _ = _inputs(monkeypatch)
    task["request"]["end"] = "2026-11-12"
    with pytest.raises(ValueError, match="完整双周期"):
        runner.run_task(task)
    task, _, _ = _inputs(monkeypatch)
    monkeypatch.setattr(
        runner, "input_dates", lambda *args: ([date(2026, 1, 5)], set(), set())
    )
    with pytest.raises(ValueError, match="真实涨跌停"):
        runner.run_task(task)
