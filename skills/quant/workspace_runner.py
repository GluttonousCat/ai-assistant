"""Run a frozen workspace draft with existing causal signal and fill engines."""

from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from skills.quant.engine import simulate
from skills.quant.models import BacktestConfig
from skills.quant.prepare import MarketSeries, _normalized, prepare_swing
from skills.quant.tranche_engine import simulate as simulate_tranches
from skills.quant.tranche_prepare import attach_confirmations
from skills.quant.tranches import TrancheConfig
from skills.quant.workspace_models import StrategyDraft, BacktestRequest
from skills.trend.swing import SwingConfig, VERSION
from tools.market.backtest_data import benchmarks, input_dates, observed_dates, quotes
from tools.market.trend_data import scan_cutoffs, scan_universe

WORKSPACE_VERSION = "workspace-60f-v1"


def execution_config(draft):
    values = draft.execution.model_dump()
    fields = BacktestConfig.__dataclass_fields__
    return BacktestConfig(
        **{k: v for k, v in values.items() if k in fields},
        max_entry_risk_pct=draft.entry.max_risk_pct,
        close_only=True,
    )


def trim_series(stock, start, end):
    selected = np.flatnonzero(
        (stock.times >= start)
        & (stock.times < pd.Timestamp(end) + pd.Timedelta(days=1))
    )
    if not len(selected):
        return None
    first, last = int(selected[0]), int(selected[-1])
    return MarketSeries(
        stock.code,
        stock.name,
        stock.observed_at,
        stock.times[first : last + 1].copy(),
        {k: v[first : last + 1].copy() for k, v in stock.data.items()},
        [
            {**sig, "i": sig["i"] - first}
            for sig in stock.signals
            if first <= sig["i"] <= last
        ],
    )


def _benchmarks(result, start, end):
    rows = benchmarks(start, end)
    indexed = {(str(r["trade_date"]), r["ts_code"]): float(r["close"]) for r in rows}
    metrics = {}
    for code in ("000300.SH", "000852.SH"):
        points = result["equity"]
        base = indexed.get((points[0]["date"], code))
        missing = []
        for point in points:
            value = indexed.get((point["date"], code))
            point.setdefault("benchmarks", {})[code] = (
                value / base if base and value else None
            )
            if not value:
                missing.append(point["date"])
        final = indexed.get((points[-1]["date"], code))
        metrics[code] = {
            "return_pct": (final / base - 1) * 100
            if base and final and not missing
            else None,
            "missing_dates": missing,
        }
    result["benchmark_metrics"] = metrics
    value = metrics["000852.SH"]["return_pct"]
    result["metrics"]["excess_csi1000_pct"] = (
        result["metrics"]["total_return_pct"] - value if value is not None else None
    )


def run_task(task, progress=lambda **fields: None):
    draft = StrategyDraft.model_validate(task["spec"])
    request = BacktestRequest.model_validate(task["request"])
    if draft.unresolved_requirements:
        raise ValueError("策略包含未解决的要求，不能执行")
    start, end = request.start.isoformat(), request.end.isoformat()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    bounds = scan_cutoffs(now)
    if any(bounds.get(key) is None for key in ("bar_end", "daily_end", "factor_date")):
        raise ValueError("本地60F、日K或复权数据尚未就绪")
    latest = min(
        pd.Timestamp(bounds[k]).date() for k in ("bar_end", "daily_end", "factor_date")
    )
    if now.hour < 15:
        latest = min(
            latest, (pd.Timestamp(now).normalize() - pd.Timedelta(days=1)).date()
        )
    if request.end > latest:
        raise ValueError(f"完整双周期数据截至{latest}，请缩短回测区间或等待盘后更新")
    days, limit_days, daily_days = input_dates(start, end)
    if not days or set(days) - limit_days or set(days) - daily_days:
        raise ValueError("区间内交易日历、日K或真实涨跌停数据不完整，请先补齐本地数据")
    members = scan_universe(end)
    members = [
        m for m in members if "ST" not in m["name"].upper() and "退" not in m["name"]
    ]
    if draft.universe == "mainboard":
        members = [
            m
            for m in members
            if m["ts_code"].startswith(
                ("000", "001", "002", "003", "600", "601", "603", "605")
            )
        ]
    elif draft.universe != "five_indices":
        members = [m for m in members if draft.universe in m["indices"]]
    members.sort(key=lambda m: m["ts_code"])
    pool_size = len(members)
    if request.sample_limit:
        members = members[: request.sample_limit]
    if not members:
        raise ValueError("当前区间没有可用成分股名单，请调整股票池或结束日期")
    progress(progress="读取本地行情与构造60F信号", completed=0, total=len(members))
    observed = observed_dates([m["ts_code"] for m in members], end)
    warmup = (
        max(pd.Timestamp("2026-01-01"), pd.Timestamp(start) - pd.Timedelta(days=180))
        .date()
        .isoformat()
    )
    entry_cfg = SwingConfig(**draft.entry.model_dump())
    staged = (
        draft.execution.entry_plan != "single" or draft.execution.exit_plan != "single"
    )
    series, excluded = [], []
    universe_audit = []
    for offset in range(0, len(members), 50):
        batch = members[offset : offset + 50]
        codes = [m["ts_code"] for m in batch]
        minute = quotes(codes, warmup, end, "60F")
        daily = quotes(codes, warmup, end, "1D")
        mg = dict(tuple(minute.groupby("ts_code"))) if not minute.empty else {}
        dg = dict(tuple(daily.groupby("ts_code"))) if not daily.empty else {}
        for member in batch:
            code = member["ts_code"]
            universe_audit.append(
                {
                    "code": code,
                    "membership_date": str(member["membership_date"]),
                    "observed_at": str(observed.get(code)),
                }
            )
            if code not in mg:
                excluded.append({"code": code, "reason": "60F原生行情缺失"})
                continue
            frame, data, issue = _normalized(dg.get(code, pd.DataFrame()))
            if not issue:
                frozen = MarketSeries(
                    code,
                    member["name"],
                    pd.Timestamp(observed.get(code) or now),
                    pd.DatetimeIndex(frame.bar_end),
                    data,
                    [],
                )
                stock, issue = prepare_swing(
                    mg.get(code, pd.DataFrame()), frozen, entry_cfg
                )
                if not issue:
                    if staged:
                        stock = attach_confirmations(mg[code], stock)
                    stock = trim_series(stock, start, end)
                    if stock is None:
                        issue = "测试区间无原生日K"
                    elif (
                        not np.isfinite(stock.data["up_limit"]).all()
                        or not np.isfinite(stock.data["down_limit"]).all()
                    ):
                        issue = "测试区间真实涨跌停不完整"
                    else:
                        series.append(stock)
            if issue:
                excluded.append({"code": code, "reason": issue})
        progress(
            progress="构造60F信号",
            completed=min(offset + 50, len(members)),
            total=len(members),
        )
    if not series:
        raise ValueError(
            "没有通过行情、复权与限价完整性检查的股票，拒绝将缺失数据当作零收益"
        )
    clock = pd.DatetimeIndex(
        [pd.Timestamp(day) + pd.Timedelta(hours=15) for day in days]
    )
    config = execution_config(draft)
    strategy_id = {
        "both": "swing_60f_v8",
        "base_rebound": "swing_reversal_v8",
        "trend_pullback": "swing_pullback_v8",
    }[draft.entry.family]
    progress(progress="模拟组合撮合与资金曲线")
    if staged:
        weights = {"single": (1.0,), "442": (0.4, 0.4, 0.2), "532": (0.5, 0.3, 0.2)}
        cfg = TrancheConfig(
            base=config,
            entry_weights=weights[draft.execution.entry_plan],
            exit_weights=weights[draft.execution.exit_plan],
            add_at_r=() if draft.execution.entry_plan == "single" else (0.5, 1.0),
            take_profit_pct=()
            if draft.execution.exit_plan == "single"
            else (draft.execution.profit_first_pct, draft.execution.profit_second_pct),
            protect_at_pct=draft.execution.profit_first_pct,
        )
        result = simulate_tranches(series, clock, strategy_id, cfg)
    else:
        result = simulate(series, clock, strategy_id, "1D", "fixed_snapshot", config)
    result.update(
        run_id=task["id"],
        suite_id=task["id"],
        strategy_id=strategy_id,
        timeframe="1D",
        universe_mode="fixed_snapshot",
        start=start,
        end=end,
        strategy_version=VERSION,
        strategy_config=asdict(entry_cfg),
        workspace_spec=draft.model_dump(),
        workspace_version=WORKSPACE_VERSION,
        diagnostics={
            "research_kind": "workspace",
            "trend_timeframe": "60F",
            "execution_schedule": "after_close",
            "eligible_for_default": False,
            "universe_size": len(members),
            "available_pool_size": pool_size,
            "usable_stocks": len(series),
            "excluded": excluded,
            "bar_count": sum(len(s.times) for s in series),
            "warmup_start": warmup,
            "universe_audit": universe_audit,
            "warnings": [
                "使用当前可获得的固定成分名单，存在成分前视与幸存者偏差",
                "2026样例已参与设计，本次为样本内研究",
                "当前简称过滤ST，无法还原完整历史ST状态",
                "复权为因子份额近似，非逐笔权益台账；跳空及跌停可突破计划止损",
                "抽样按股票代码取前N只，仅用于快速检查，不能代表全池表现"
                if request.sample_limit
                else "本次按所选成分股票池全量运行",
                "日K仅供下一交易日开盘执行；60F信号需当日四根完整且收盘未失效",
            ],
        },
    )
    _benchmarks(result, start, end)
    return result
