"""Index-aware 442 research, preserving stops and frozen stock entry definitions.

Finite development selection followed by fixed chronological checks. All 2026
segments were already seen in earlier work, so none is independent OOS evidence.
"""

import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import pickle
from uuid import uuid4

import numpy as np
import pandas as pd

from core.config import get_config
from core.logger import get_logger
from jobs.backtest_mtf import window_series, WINDOWS
from jobs.research_mainboard_60f import ENTRY_PROFILES, qualified, score, write_json
from jobs.research_tranches import PLANS
from skills.quant.market_context import (
    MarketPolicy,
    index_context,
    attach_market,
    apply_policy,
    VERSION,
)
from skills.quant.tranche_engine import simulate
from skills.quant.tranche_prepare import confirm_signal_close
from skills.quant.prepare import MarketSeries
from skills.quant.signal_priority import attach_priority, prioritize
from skills.quant.report import write_result, with_benchmarks
from skills.trend.swing import VERSION as ENTRY_VERSION

logger = get_logger(__name__)
POLICIES = {
    "baseline": None,
    "fast": MarketPolicy("any_fast"),
    "trend": MarketPolicy("any_trend"),
    "trend_entry_only": MarketPolicy("any_trend", guard_adds=False),
    "both": MarketPolicy("both_trend"),
    "avoid_down": MarketPolicy("avoid_both_down"),
    "daily": MarketPolicy("daily_trend"),
    "trend_relative": MarketPolicy("any_trend", relative_filter=True),
    "daily_relative": MarketPolicy("daily_trend", relative_filter=True),
    "soft_only": MarketPolicy("none", guard_adds=False, soft_grace_days=1),
    "trend_soft": MarketPolicy("any_trend", soft_grace_days=1),
    "trend_relative_soft": MarketPolicy(
        "any_trend", relative_filter=True, soft_grace_days=1
    ),
    "daily_relative_soft": MarketPolicy(
        "daily_trend", relative_filter=True, soft_grace_days=1
    ),
}
PLAN = PLANS["entry_exit_442"]
FOLDER = "market_v11"
REFERENCE = (
    "output/quant/mtf_v51_research/full/structure_2r/1D_mtf_dual_v5_fixed_snapshot.json"
)


def timing_cases():
    """Finite follow-up: close retention, residual strength, adds and exits.

    No stronger-market entry requirement: v11 rejected that hypothesis. The
    cash/risk/stop limits and 442 fractions are identical in every case.
    """
    cases = {"baseline": dict(policy=None, plan=PLAN, hold_close=False)}
    profits = {
        "quick": ((3.0, 5.0), 3.0),
        "protect": ((5.0, 8.0), 3.0),
        "runner": ((8.0, 13.0), 5.0),
    }
    for hold in (False, True):
        for relative in (False, True):
            for exit_name, (targets, protect) in profits.items():
                for early in (False, True):
                    for days in (20, 40):
                        name = f"{'hold' if hold else 'any'}_{'relative' if relative else 'plain'}_{exit_name}_{'early' if early else 'normal'}_{days}"
                        cases[name] = dict(
                            policy=MarketPolicy("none", relative_filter=relative),
                            hold_close=hold,
                            plan=replace(
                                PLAN,
                                base=replace(PLAN.base, max_holding_bars=days),
                                add_at_r=(0.25, 0.5) if early else PLAN.add_at_r,
                                take_profit_pct=targets,
                                protect_at_pct=protect,
                            ),
                        )
    return cases


def filter_source(series, hold_close):
    if not hold_close:
        return series
    return [
        MarketSeries(
            s.code,
            s.name,
            s.observed_at,
            s.times.copy(),
            dict(s.data),
            [sig for sig in s.signals if sig.get("close_held")],
        )
        for s in series
    ]


def priority_cases():
    cases = {"baseline": dict(policy=None, plan=PLAN, hold_close=False)}
    for style in ("support", "relative", "blended"):
        for extension in (1.0, 2.5):
            for hold in (False, True):
                name = f"{style}_ext{extension:g}_{'hold' if hold else 'any'}"
                cases[name] = dict(
                    policy=MarketPolicy("none"),
                    plan=PLAN,
                    hold_close=hold,
                    priority=style,
                    max_extension=extension,
                )
    return cases


def patience_cases():
    cases = {"baseline": dict(policy=None, plan=PLAN, hold_close=False)}
    for wait in (0, 8, 12):
        for days in (20, 40):
            for targets in ((5.0, 8.0), (8.0, 13.0)):
                name = f"patience{wait}_{days}_profit{targets[0]:g}"
                cases[name] = dict(
                    policy=MarketPolicy("none"),
                    hold_close=False,
                    plan=replace(
                        PLAN,
                        take_profit_pct=targets,
                        base=replace(
                            PLAN.base,
                            trend_exit=wait > 0,
                            min_trend_holding_bars=wait or 4,
                            max_holding_bars=days,
                        ),
                    ),
                )
    return cases


def load_indices(folder, root):
    reference = json.loads((root / REFERENCE).read_text(encoding="utf-8"))
    calendar = pd.DatetimeIndex(
        [pd.Timestamp(p["date"]) + pd.Timedelta(hours=15) for p in reference["equity"]]
    )
    frames = {}
    audit = {}
    for code in ["000300.SH", "000852.SH"]:
        path = folder / "indices" / f"{code}.pkl"
        frame = pd.read_pickle(path)
        frame["bar_end"] = pd.to_datetime(frame.bar_end)
        if len(frame) != len(calendar) * 4:
            raise ValueError("指数分钟未覆盖完整交易日历")
        closing = (
            frame[frame.bar_end.dt.hour.eq(15)]
            .set_index("bar_end")
            .close.reindex(calendar)
        )
        expected = np.asarray(
            [p["benchmarks"][code] for p in reference["equity"]], dtype=float
        )
        if closing.isna().any():
            raise ValueError("指数缺少收盘原生条目")
        error = float(
            np.max(np.abs(closing.to_numpy() / closing.iloc[0] / expected - 1))
        )
        if error > 0.005:
            raise ValueError(f"{code}分钟与冻结日线偏差超过0.5%")
        frames[code] = frame
        audit[code] = dict(
            rows=len(frame),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            max_daily_relative_error=error,
            first=str(frame.bar_end.min()),
            last=str(frame.bar_end.max()),
            source="akshare-sina",
        )
    return frames, calendar, audit


def declare(folder, root, audit, cases=None, study="context"):
    source = root / "output/quant/tranches_v10/prepared.pkl"
    files = [
        "skills/quant/market_context.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranches.py",
    ]
    value = json.loads(
        json.dumps(
            dict(
                version=VERSION,
                indices=audit,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                code_sha256={
                    f: hashlib.sha256((root / f).read_bytes()).hexdigest()
                    for f in files
                },
                entry_config=asdict(ENTRY_PROFILES["balanced"]),
                plan=asdict(PLAN),
                policies={k: asdict(v) if v else None for k, v in POLICIES.items()},
                windows=WINDOWS,
                selection="Jan-Jun only: >=30 closed campaigns, positive return, PF>1, MDD<=8%, worst>=-8%; maximize return/max(MDD,1). No qualifying policy means keep baseline and continue research.",
                relative_evidence="stock last4 native60F return minus lagged 60-bar beta times equal-weight index return; beta uses >=40 prior samples, clipped [0,3], no fabricated beta fallback.",
                systemic_panic="Both indices negative over 4 native bars, mean return<=-1.5%; only soft M21 failure can receive one additional observation session if stock residual>=0 and price within 1ATR of M21. Stops, timeout and failed rebounds do not receive relief.",
                target="Full 2026 portfolio>=5%, MDD<=8%, >=30 campaigns, worst campaign>=-8%, doubled slippage positive, same universe/cost/risk constraints.",
                limitations=[
                    "All 2026 segments were already inspected; not independent unseen validation.",
                    "Fixed current membership/current non-ST and factor-share approximation retain previous biases.",
                    "Index daily trend is macro context, never a stock daily-candle gate; missing index context blocks entries/adds and never grants delayed exits.",
                    "4% planned stop and .35% account risk are not guaranteed losses under next-open gaps/price limits.",
                ],
            )
        )
    )
    if cases is not None:
        is_priority = any("priority" in v for v in cases.values())
        value.update(
            version="market-relative-signal-priority-v13"
            if is_priority
            else "market-close-and-tranche-timing-v12",
            cases={
                k: dict(
                    policy=asdict(v["policy"]) if v["policy"] else None,
                    plan=asdict(v["plan"]),
                    hold_close=v["hold_close"],
                    **(
                        {"priority": v["priority"], "max_extension": v["max_extension"]}
                        if "priority" in v
                        else {}
                    ),
                )
                for k, v in cases.items()
            },
            followup="49 declared cases: v10 control plus close retention x market residual strength x 3 profit plans x 2 add timings x 2 holding limits. Same original stops and risk. No soft-exit grace after its negative v11 result.",
        )
        value.pop("policies")
        if is_priority:
            value["followup"] = (
                "13 declared cases: original control plus support/relative20/blended ranking x max M13 extension 1/2.5ATR x close retention. Fixed original 442, 5/8% exits and stops. No calendar/code filters and no changed capital risk."
            )
        for f in ["skills/quant/tranche_prepare.py", "jobs/research_market_context.py"]:
            value["code_sha256"][f] = hashlib.sha256(
                (root / f).read_bytes()
            ).hexdigest()
        if is_priority:
            f = "skills/quant/signal_priority.py"
            value["code_sha256"][f] = hashlib.sha256(
                (root / f).read_bytes()
            ).hexdigest()
        value = json.loads(json.dumps(value))
    if study == "patience":
        value["version"] = "fixed-stop-structural-exit-patience-v14"
        value["followup"] = (
            "13 cases: control plus soft structural exit disabled/held at least8/12 sessions x timeout20/40 x profit5/8 or8/13. Original price stop, 5% cost protection, 442 adds, .35% risk and 4% entry distance unchanged. No blanket relaxation of price stops."
        )
        f = "skills/quant/report.py"
        value["code_sha256"][f] = hashlib.sha256((root / f).read_bytes()).hexdigest()
    path = folder / "declaration.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
        raise ValueError("Frozen market experiment changed")
    if not path.exists():
        write_json(path, value)
    return value


def prepare(folder, root, frames):
    with (root / "output/quant/tranches_v10/prepared.pkl").open("rb") as stream:
        source = pickle.load(stream)
    ctx, ret, ret4 = index_context(frames)
    ctx.to_csv(folder / "index_states.csv", index_label="bar_end")
    stocks = []
    for n, s in enumerate(source["series"]["old"], 1):
        minute = pd.read_pickle(
            root / "output/quant/mainboard_60f_v8/minutes" / f"{s.code}.pkl"
        )
        stocks.append(attach_market(minute, s, ctx, ret, ret4))
        if n % 250 == 0 or n == len(source["series"]["old"]):
            logger.info("Index-aware evidence %s/%s", n, len(source["series"]["old"]))
    payload = dict(
        series=stocks,
        universe_size=source["universe_size"],
        excluded=source["excluded"],
    )
    with (folder / "prepared.pkl").open("wb") as stream:
        pickle.dump(payload, stream, pickle.HIGHEST_PROTOCOL)
    return payload


def run(folder, root, payload, declared, calendar, cases=None):
    suite = str(uuid4())
    reference = json.loads((root / REFERENCE).read_text(encoding="utf-8"))["equity"]
    cases = cases or {
        k: dict(policy=v, plan=PLAN, hold_close=False) for k, v in POLICIES.items()
    }

    def calculate(name, window, stress=False):
        case = cases[name]
        policy = case["policy"]
        start, end = WINDOWS[window]
        source = (
            payload["series"]
            if policy is None
            else [apply_policy(s, policy) for s in payload["series"]]
        )
        source = filter_source(source, case["hold_close"])
        if "priority" in case:
            source = [
                prioritize(s, case["priority"], case["max_extension"]) for s in source
            ]
        stocks = window_series(source, start, end)
        clock = calendar[
            (calendar >= start) & (calendar < pd.Timestamp(end) + pd.Timedelta(days=1))
        ]
        plan = case["plan"]
        cfg = replace(plan, base=replace(plan.base, slippage=0.001)) if stress else plan
        r = simulate(stocks, clock, "swing_60f_v8", cfg, market_policy=policy)
        r.update(
            run_id=str(uuid4()),
            suite_id=suite,
            strategy_id="swing_60f_v8",
            strategy_version=ENTRY_VERSION,
            strategy_config=asdict(ENTRY_PROFILES["balanced"]),
            timeframe="1D",
            universe_mode="fixed_snapshot",
            start=start,
            end=end,
            diagnostics=dict(
                trend_timeframe="60F",
                entry_timeframe="1D",
                execution_schedule="after_close",
                research_kind="market_priority" if "priority" in case else "market_442",
                candidate_count=len(cases),
                research_variant=name + ("__cost_stress" if stress else ""),
                exit_profile="entry_exit_442",
                market_policy=asdict(policy) if policy else None,
                hold_signal_close=case["hold_close"],
                signal_priority=case.get("priority"),
                max_extension_atr=case.get("max_extension"),
                universe_size=payload["universe_size"],
                usable_stocks=len(stocks),
                bar_count=sum(len(s.times) for s in stocks),
                in_sample=True,
                research_selected=False,
                eligible_for_default=False,
                research_status="experimental",
                warnings=declared["limitations"],
            ),
        )
        r["metrics"]["worst_trade_return_pct"] = min(
            (t["return_pct"] for t in r["trades"]), default=None
        )
        assert (
            abs(
                r["metrics"]["ending_equity"]
                - (
                    r["config"]["initial_cash"]
                    + sum(t["pnl"] for t in r["trades"])
                    + sum(
                        p["unrealized_pnl"] + p.get("partial_realized_pnl", 0)
                        for p in r["open_positions"]
                    )
                )
            )
            < 1e-5
        )
        return with_benchmarks(r, reference)

    train = {}
    for name in cases:
        r = calculate(name, "development")
        train[name] = r
        write_result(folder / "development" / name, r)
        logger.info(
            "Market development %s return=%.3f drawdown=%.3f cycles=%s grace=%s",
            name,
            r["metrics"]["total_return_pct"],
            r["metrics"]["max_drawdown_pct"],
            r["metrics"]["closed_trades"],
            r["execution_audit"].get("systemic_soft_grace", 0),
        )
    eligible = [n for n, r in train.items() if qualified(r)]
    chosen = (
        max(eligible, key=lambda n: score(train[n]["metrics"]))
        if eligible
        else "baseline"
    )
    selection = dict(
        selected=chosen,
        qualified=bool(eligible),
        qualifying_count=len(eligible),
        selected_on="development only",
        ranking=[
            dict(name=n, qualified=bool(qualified(r)), metrics=r["metrics"])
            for n, r in sorted(
                train.items(), key=lambda x: score(x[1]["metrics"]), reverse=True
            )
        ],
    )
    write_json(folder / "selection.json", selection)
    comparisons = {}
    fulls = {}
    for name in cases:
        comparisons[name] = {"development": train[name]["metrics"]}
        for window in ["validation", "time_holdout", "full"]:
            r = calculate(name, window)
            r["diagnostics"]["training_qualified"] = bool(qualified(train[name]))
            r["diagnostics"]["selected_on_development"] = name == chosen
            write_result(folder / name / window, r)
            comparisons[name][window] = r["metrics"]
            if window == "full":
                fulls[name] = r
    stress = calculate(chosen, "full", stress=True)
    write_result(folder / "selected_cost_stress", stress)
    full = fulls[chosen]
    m = full["metrics"]
    passed = bool(
        selection["qualified"]
        and m["total_return_pct"] >= 5
        and m["max_drawdown_pct"] <= 8
        and m["closed_trades"] >= 30
        and (m["worst_trade_return_pct"] or 0) >= -8
        and stress["metrics"]["total_return_pct"] > 0
    )
    # Every alternative remains visible; none is picked from its full return.
    summary = dict(
        selection=selection,
        variants=comparisons,
        cost_stress=stress["metrics"],
        historical_target_met=passed,
        independent_validation=False,
    )
    write_json(folder / "comparison.json", summary)
    lines = [
        "# 指数环境与442固定对照",
        "",
        "**2026样本内研究；只按开发段选择，后续区间早已看过，不属于独立样本外。**",
        f"开发段选择：{chosen}；合格候选数：{len(eligible)}；5%及风险/成本门槛通过：{passed}。",
        "",
        "| 指数策略 | 累计收益 | 最大回撤 | 完成周期 | 最差周期 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, r in fulls.items():
        x = r["metrics"]
        worst = x["worst_trade_return_pct"]
        lines.append(
            f"| {name} | {x['total_return_pct']:.2f}% | {x['max_drawdown_pct']:.2f}% | {x['closed_trades']} | {worst:.2f}% |"
            if worst is not None
            else f"| {name} | 无交易 | — | 0 | — |"
        )
    lines += [
        "",
        f"选定方案双倍滑点收益{stress['metrics']['total_return_pct']:.2f}%。",
        "指数：沪深300/中证1000原生60F，与冻结Tushare日线归一收盘核对。个股仍是60F入场、盘后计划次日执行；442、10%单股计划与0.35%风险预算均不改变。止盈、增仓和超时参数逐候选冻结，见声明和报告config。",
        "同时下跌且均值跌幅至少1.5%时，相对抗跌并未触及原止损的软离场只允许一次额外观察交易日；硬止损、超时和失败反弹不延迟。缺失指数不能放宽离场。",
        "原始资料：[沪深300](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000300factsheet.pdf)、[中证1000](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/20231208175402-000852_Index_Methodology_cn.pdf)。指数定义不证明策略盈利。",
        "参数/数据/代码指纹见declaration.json，完整候选与逐笔保留在各目录；没有按个股代码、未来收益或有利日期筛掉亏损样本。",
    ]
    (folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            dict(
                selected=chosen,
                qualified=selection["qualified"],
                historical_target_met=passed,
                full=m,
                stress=stress["metrics"],
            ),
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-indices", action="store_true")
    parser.add_argument("--declare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--publish-only", action="store_true")
    parser.add_argument("--publish-variant")
    parser.add_argument("--trade-page-size", type=int, default=25)
    parser.add_argument(
        "--study",
        choices=["context", "timing", "priority", "patience"],
        default="context",
    )
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    cases = (
        timing_cases()
        if args.study == "timing"
        else priority_cases()
        if args.study == "priority"
        else patience_cases()
        if args.study == "patience"
        else None
    )
    folders = dict(
        context=FOLDER,
        timing="market_timing_v12",
        priority="market_priority_v13",
        patience="market_patience_v14",
    )
    folder = root / "output/quant" / folders[args.study]
    folder.mkdir(parents=True, exist_ok=True)
    if args.publish_variant and not args.publish_only:
        parser.error("--publish-variant requires --publish-only")
    if not 1 <= args.trade_page_size <= 500:
        parser.error("--trade-page-size must be 1..500")
    if args.publish_only:
        # Publish frozen artifacts independently from current experiment code:
        # later CLI/report additions must not invalidate a completed experiment.
        from jobs.backtest_quant import _database_unit
        from storage.repositories.backtest import publish
        from storage.schema.quant import ensure_quant_schema

        summary = json.loads((folder / "comparison.json").read_text(encoding="utf-8"))
        frozen = json.loads((folder / "declaration.json").read_text(encoding="utf-8"))
        files = {
            k: next((folder / k / "full").glob("1D_*.json"))
            for k in summary["variants"]
        }
        files["selected_cost_stress"] = next(
            (folder / "selected_cost_stress").glob("1D_*.json")
        )
        if args.publish_variant and args.publish_variant not in files:
            parser.error("unknown frozen variant")
        reference = json.loads((root / REFERENCE).read_text(encoding="utf-8"))["equity"]
        ensure_quant_schema()
        for name in [args.publish_variant] if args.publish_variant else files:
            original = json.loads(files[name].read_text(encoding="utf-8"))
            report = with_benchmarks(original, reference)
            report["diagnostics"] = {
                **report["diagnostics"],
                "candidate_count": len(frozen.get("cases", frozen.get("policies", {}))),
                "research_study": args.study,
                "historical_target_met": summary["historical_target_met"],
                "benchmark_enrichment": "Date-keyed frozen price-index closes, rebased per report window; strategy metrics and trades unchanged.",
            }
            assert (
                report["metrics"] == original["metrics"]
                and report["trades"] == original["trades"]
            )
            write_result(folder / "published" / name, report)
            _database_unit(
                lambda: publish(report, trade_page_size=args.trade_page_size)
            )
            logger.info("Published market research %s %s", args.study, name)
        print("Published frozen market reports; performance and defaults unchanged.")
        return
    if args.fetch_indices:
        from tools.market.sina_transport import fetch_sina_history

        index_cache = root / "output/quant" / FOLDER / "indices"
        index_cache.mkdir(parents=True, exist_ok=True)
        for code in ["000300.SH", "000852.SH"]:
            target = index_cache / f"{code}.pkl"
            if not target.exists():
                fetch_sina_history(code, "2026-01-01", "2026-11-12").to_pickle(target)
    index_folder = root / "output/quant" / FOLDER
    frames, calendar, audit = load_indices(index_folder, root)
    declared = declare(folder, root, audit, cases, args.study)
    if args.declare_only:
        print(
            f"Frozen {len(cases) if cases else len(POLICIES)} cases; 442 and risk/cost assumptions unchanged."
        )
        return
    if args.resume:
        with (folder / "prepared.pkl").open("rb") as stream:
            payload = pickle.load(stream)
    elif args.study == "priority":
        with (root / "output/quant/market_timing_v12/prepared.pkl").open(
            "rb"
        ) as stream:
            original = pickle.load(stream)
        market20 = pd.concat(
            [
                f.set_index("bar_end").close.pct_change(20, fill_method=None)
                for f in frames.values()
            ],
            axis=1,
        ).mean(axis=1, skipna=False)
        stocks = []
        for n, s in enumerate(original["series"], 1):
            minute = pd.read_pickle(
                root / "output/quant/mainboard_60f_v8/minutes" / f"{s.code}.pkl"
            )
            stocks.append(attach_priority(minute, s, market20))
            if n % 500 == 0 or n == len(original["series"]):
                logger.info("Priority evidence %s/%s", n, len(original["series"]))
        payload = {**original, "series": stocks}
        with (folder / "prepared.pkl").open("wb") as stream:
            pickle.dump(payload, stream, pickle.HIGHEST_PROTOCOL)
    elif args.study == "patience":
        with (index_folder / "prepared.pkl").open("rb") as stream:
            payload = pickle.load(stream)
        with (folder / "prepared.pkl").open("wb") as stream:
            pickle.dump(payload, stream, pickle.HIGHEST_PROTOCOL)
    elif cases:
        with (index_folder / "prepared.pkl").open("rb") as stream:
            original = pickle.load(stream)
        stocks = []
        for s in original["series"]:
            minute = pd.read_pickle(
                root / "output/quant/mainboard_60f_v8/minutes" / f"{s.code}.pkl"
            )
            stocks.append(confirm_signal_close(minute, s))
        payload = {**original, "series": stocks}
        with (folder / "prepared.pkl").open("wb") as stream:
            pickle.dump(payload, stream, pickle.HIGHEST_PROTOCOL)
    else:
        payload = prepare(folder, root, frames)
    run(folder, root, payload, declared, calendar, cases)


if __name__ == "__main__":
    main()
