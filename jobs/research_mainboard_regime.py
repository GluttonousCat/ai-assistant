"""Second declared experiment after the first 36 failed on development data.

Adds 60F breadth/repair evidence; compares after-close with precommitted broker
conditions explicitly. Does not inspect or select using the first holdout.
"""

import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import pickle
from uuid import uuid4

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from jobs.backtest_mtf import window_series, WINDOWS
from jobs.research_mainboard_60f import (
    ENTRY_PROFILES,
    EXIT_PROFILES,
    LIMITS,
    score,
    qualified,
    risk_eligible,
    write_json,
)
from skills.quant.engine import simulate
from skills.quant.prepare import MarketSeries, swing_frame
from skills.quant.report import write_result
from skills.trend.regime import closing_context, breadth, allowed_signals

logger = get_logger(__name__)
VERSION = "60f-divergence-pullback-regime-v8.1.1"
THRESHOLDS = [0.45, 0.6]
EXITS = ["scheduled_reversal", "scheduled_3r_20d"]
MODES = ["after_close", "precommitted"]


def declaration(folder, source):
    value = json.loads(
        json.dumps(
            dict(
                version=VERSION,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                prior_experiment="mainboard_60f_v8, 36 candidates, zero development qualification; first holdout not inspected for this design",
                candidates=24,
                entries={k: asdict(v) for k, v in ENTRY_PROFILES.items()},
                breadth_thresholds=THRESHOLDS,
                execution_modes=MODES,
                exits={k: asdict(EXIT_PROFILES[k]) for k in EXITS},
                constraints=LIMITS,
                windows=WINDOWS,
                selection="development only, same qualification and Calmar as first experiment; no later-window reselection",
                stock_checks="60F reversal: close>M13, M8>M13, positive histogram; pullback: close>M13, M8>M13>M21, M21 rises over 8 bars",
                limitations=[
                    "Fixed current universe/current non-ST and known 2026 examples; not independent OOS.",
                    "Second development search after 36 failed candidates; total search count 60.",
                    "Breadth is the 2333-stock mainboard research pool, not the whole A-share market.",
                    "Precommitted requires broker conditional orders; it is not after-work discretionary execution.",
                    "Stops/targets/fills are approximations; overnight gaps and price limits can exceed planned risk.",
                ],
            )
        )
    )
    path = folder / "declaration.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
        raise ValueError("Regime experiment declaration changed")
    if not path.exists():
        write_json(path, value)
    return value


def prepare(folder, source):
    with source.open("rb") as stream:
        base = pickle.load(stream)
    contexts = {}
    minute_root = source.parent / "minutes"
    for n, s in enumerate(base["series"]["responsive"], 1):
        frame, issue = swing_frame(pd.read_pickle(minute_root / f"{s.code}.pkl"), s)
        if issue:
            raise ValueError(issue)
        contexts[s.code] = closing_context(frame)
        if n % 250 == 0:
            logger.info("Regime prepare %s/%s", n, len(base["series"]["responsive"]))
    market = breadth(contexts)
    market.to_csv(folder / "breadth.csv", index_label="bar_end")
    series = {}
    for entry, stocks in base["series"].items():
        for threshold in THRESHOLDS:
            key = f"{entry}__{threshold}"
            series[key] = [
                MarketSeries(
                    s.code,
                    s.name,
                    s.observed_at,
                    s.times,
                    s.data,
                    allowed_signals(s, contexts[s.code], market, threshold),
                )
                for s in stocks
            ]
    prepared = dict(
        series=series, universe_size=base["universe_size"], excluded=base["excluded"]
    )
    with (folder / "prepared.pkl").open("wb") as stream:
        pickle.dump(prepared, stream, pickle.HIGHEST_PROTOCOL)
    return prepared


def run(folder, prepared, declared, root):
    old = json.loads(
        (
            root
            / "output/quant/reversal_v6_1/full/1D_mtf_reversal_v6_fixed_snapshot.json"
        ).read_text(encoding="utf-8")
    )
    days = pd.DatetimeIndex(
        [pd.Timestamp(p["date"]) + pd.Timedelta(hours=15) for p in old["equity"]]
    )
    suite = str(uuid4())

    def calculate(key, exit_name, mode, window, stress=False):
        entry, threshold = key.split("__")
        cfg = replace(
            EXIT_PROFILES[exit_name],
            close_only=mode == "after_close",
            slippage=0.001 if stress else 0.0005,
        )
        start, end = WINDOWS[window]
        stocks = window_series(prepared["series"][key], start, end)
        clock = days[
            (days >= start) & (days < pd.Timestamp(end) + pd.Timedelta(days=1))
        ]
        r = simulate(stocks, clock, "swing_60f_v8", "1D", "fixed_snapshot", cfg)
        r.update(
            run_id=str(uuid4()),
            suite_id=suite,
            strategy_id="swing_60f_v8",
            strategy_version=VERSION,
            strategy_config={
                **asdict(ENTRY_PROFILES[entry]),
                "breadth_threshold": float(threshold),
                "regime_guard": True,
            },
            timeframe="1D",
            universe_mode="fixed_snapshot",
            start=start,
            end=end,
            diagnostics=dict(
                trend_timeframe="60F",
                entry_timeframe="1D",
                execution_schedule=mode,
                board="mainboard",
                research_window=window,
                exit_profile=exit_name,
                entry_profile=entry,
                universe_size=prepared["universe_size"],
                usable_stocks=len(stocks),
                bar_count=sum(len(s.times) for s in stocks),
                in_sample=True,
                warnings=declared["limitations"],
                research_selected=window == "full" and not stress,
            ),
        )
        r["metrics"]["worst_trade_return_pct"] = min(
            (t["return_pct"] for t in r["trades"]), default=None
        )
        return r

    candidates = []
    for key in prepared["series"]:
        for mode in MODES:
            for exit_name in EXITS:
                r = calculate(key, exit_name, mode, "development")
                label = f"{key}__{mode}__{exit_name}"
                write_result(folder / "development" / label, r)
                candidates.append((key, exit_name, mode, r))
                logger.info(
                    "Regime development %s/24 %s return=%.3f drawdown=%.3f trades=%s",
                    len(candidates),
                    label,
                    r["metrics"]["total_return_pct"],
                    r["metrics"]["max_drawdown_pct"],
                    r["metrics"]["closed_trades"],
                )
    eligible = [c for c in candidates if qualified(c[3])]
    pool = eligible or [c for c in candidates if risk_eligible(c[3])] or candidates
    chosen = max(pool, key=lambda c: (score(c[3]["metrics"]), c[:3]))
    key, exit_name, mode, train = chosen
    selected = dict(
        entry=key,
        exit_profile=exit_name,
        execution_schedule=mode,
        qualified=qualified(train),
        qualifying_count=len(eligible),
        selected_on="development only",
        candidate_count=24,
        total_searched=60,
        ranking=[
            dict(
                entry=k,
                exit_profile=e,
                execution_schedule=m,
                qualified=qualified(r),
                metrics=r["metrics"],
            )
            for k, e, m, r in sorted(
                candidates, key=lambda c: score(c[3]["metrics"]), reverse=True
            )
        ],
    )
    write_json(folder / "selection.json", selected)
    reports = {"development": train}
    for window in ["validation", "time_holdout", "full"]:
        r = calculate(key, exit_name, mode, window)
        r["diagnostics"]["training_qualified"] = selected["qualified"]
        write_result(folder / "selected" / window, r)
        reports[window] = r
    stress = calculate(key, exit_name, mode, "full", stress=True)
    stress["diagnostics"]["training_qualified"] = selected["qualified"]
    write_result(folder / "selected" / "cost_stress", stress)
    # Always assess the same selected thresholds with after-work exits too.
    alternate = calculate(
        key,
        exit_name,
        "after_close" if mode == "precommitted" else "precommitted",
        "full",
    )
    alternate["diagnostics"]["research_selected"] = False
    alternate["diagnostics"]["training_qualified"] = selected["qualified"]
    write_result(folder / "selected" / "execution_comparison", alternate)
    full = reports["full"]["metrics"]
    met = bool(
        selected["qualified"]
        and full["total_return_pct"] >= 5
        and full["max_drawdown_pct"] <= 8
        and (full["worst_trade_return_pct"] or 0) >= -8
        and stress["metrics"]["total_return_pct"] > 0
    )
    summary = dict(
        selection={k: v for k, v in selected.items() if k != "ranking"},
        windows={k: r["metrics"] for k, r in reports.items()},
        cost_stress=stress["metrics"],
        alternate_execution=alternate["metrics"],
        target_met=met,
        independent_validation=False,
    )
    write_json(folder / "comparison.json", summary)
    lines = [
        "# 主板60F第二轮：市场广度与执行方式",
        "",
        "**2026样本内研究；两轮共60组参数，不属于独立样本外验证。**",
        "",
        f"目标与风险门槛通过：{met}；开发段资格：{selected['qualified']}。",
        f"冻结参数：{key} / {exit_name} / {mode}。",
        "",
        "| 区间 | 净收益 | 回撤 | 平仓数 | 最差单笔 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, m in {
        **summary["windows"],
        "cost_stress": summary["cost_stress"],
        "alternate_execution": summary["alternate_execution"],
    }.items():
        worst = m["worst_trade_return_pct"]
        lines.append(
            f"| {name} | {m['total_return_pct']:.2f}% | {m['max_drawdown_pct']:.2f}% | {m['closed_trades']} | {worst:.2f}% |"
            if worst is not None
            else f"| {name} | 无成交 | — | 0 | — |"
        )
    lines += [
        "",
        "after_close：盘后收盘检查阈值、次日开盘执行；precommitted：提前挂券商条件单，盘中按预设价触发，仅为无盘口的保守撮合模型。不能把后者称为盘后手动执行。",
        "全部选股条件来自60F。主板原五指数池2333只当前非ST、真实因子与限价；单股10%、最多10只、计划风险0.35%、开盘风险距离4%；跳空及跌停可能超出。",
        "36组首轮失败后，仅基于开发段设计第二轮24组；只在1—6月选择，后续固定检查，没有按全年收益重选。",
    ]
    (folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--publish-only", action="store_true")
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    source = root / "output/quant/mainboard_60f_v8/prepared.pkl"
    folder = root / "output/quant/mainboard_60f_regime_v811"
    folder.mkdir(parents=True, exist_ok=True)
    declared = declaration(folder, source)
    if args.declare_only:
        print(
            "Declared second experiment: 24 development candidates, 60 total searched."
        )
        return
    if args.publish_only:
        from jobs.backtest_quant import _database_unit
        from storage.repositories.backtest import publish, annotate_diagnostics
        from storage.schema.quant import ensure_quant_schema

        ensure_quant_schema()
        selected = json.loads((folder / "selection.json").read_text(encoding="utf-8"))
        for window in ["full", "cost_stress", "execution_comparison"]:
            (path,) = (folder / "selected" / window).glob("*.json")
            r = json.loads(path.read_text(encoding="utf-8"))
            r["diagnostics"].update(
                training_qualified=selected["qualified"],
                research_selected=window == "full",
            )
            write_json(path, r)
            _database_unit(lambda: publish(r))
            _database_unit(lambda: annotate_diagnostics(r["run_id"], r["diagnostics"]))
        print(
            "Published regime research, cost stress and explicit execution comparison."
        )
        return
    if args.resume:
        with (folder / "prepared.pkl").open("rb") as stream:
            prepared = pickle.load(stream)
    else:
        prepared = prepare(folder, source)
    run(folder, prepared, declared, root)


if __name__ == "__main__":
    main()
