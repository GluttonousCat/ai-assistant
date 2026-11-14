"""Bounded mainboard research: 60F setups, after-close plans, next-open fills.

Declare the finite grid BEFORE running it. Select only on Jan-Jun; later windows
are assessed once. Known 2026 examples/current membership preclude a true OOS
claim. Failure to reach the target is a valid, permanently recorded result.
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
from jobs.cache_mainboard_minutes import is_mainboard
from jobs.backtest_mtf import window_series, WINDOWS, START, END
from skills.quant.engine import simulate
from skills.quant.models import BacktestConfig
from skills.quant.prepare import prepare_swing
from skills.quant.report import write_result
from skills.trend.swing import SwingConfig, VERSION

logger = get_logger(__name__)
ENTRY_PROFILES = {
    "responsive": SwingConfig(min_body_atr=0.15, divergence_improvement_atr=0.03),
    "balanced": SwingConfig(
        min_body_atr=0.25,
        min_volume_ratio=1,
        divergence_improvement_atr=0.06,
        pullback_depth_atr=2.5,
    ),
    "selective": SwingConfig(
        min_body_atr=0.4,
        min_volume_ratio=1.2,
        divergence_improvement_atr=0.1,
        pullback_depth_atr=2,
        max_risk_pct=3,
    ),
}
BASE = BacktestConfig(
    risk_budget_pct=0.35, max_entry_risk_pct=4, close_only=True, trend_exit=False
)
EXIT_PROFILES = {
    "scheduled_2r_10d": replace(BASE, take_profit_r=2, max_holding_bars=10),
    "scheduled_3r_20d": replace(BASE, take_profit_r=3, max_holding_bars=20),
    "scheduled_protected": replace(
        BASE,
        exit_policy="protected",
        trend_exit=True,
        take_profit_r=3,
        max_holding_bars=20,
    ),
    "scheduled_reversal": replace(
        BASE,
        exit_policy="reversal",
        trend_exit=True,
        protect_after_r=1,
        max_holding_bars=20,
    ),
}
STRATEGIES = ["swing_60f_v8", "swing_reversal_v8", "swing_pullback_v8"]
LIMITS = dict(
    min_closed_trades=30,
    max_drawdown_pct=8,
    worst_trade_pct=-8,
    min_profit_factor=1,
    target_return_pct=5,
)


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def preregister(folder, source):
    declaration = dict(
        version=VERSION,
        start=START,
        end=END,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        entry_profiles={k: asdict(v) for k, v in ENTRY_PROFILES.items()},
        exit_profiles={k: asdict(v) for k, v in EXIT_PROFILES.items()},
        strategies=STRATEGIES,
        candidate_count=36,
        windows=WINDOWS,
        constraints=LIMITS,
        selection="Jan-Jun only; positive return, PF>1, >=30 trades, MDD<=8%, worst trade>=-8%; maximize return/max(MDD,1). If none pass, evaluate best risk-eligible candidate as failed experiment only.",
        execution="All price/MA/MACD predicates are 60F; aggregate at close, next-session open. Stops and targets checked at close, never assume intraday fills.",
        universe="2026 frozen five-index union mainboard current non-ST, not all listed mainboard",
        limitations=[
            "Current membership and current non-ST imply lookahead/survivorship bias.",
            "Known 2026 examples influenced design; time holdout is not independent unseen data.",
            "Factor-share approximation, not a corporate-action cash ledger.",
            "4% is a planned distance, not a guaranteed realized loss cap.",
            "Minute data refreshed from same Sina source; daily execution/factors/limits frozen from warehouse.",
        ],
    )
    # JSON round-trips tuples (the frozen windows) as lists. Compare canonical
    # values so a resume cannot spuriously reject an unchanged declaration.
    declaration = json.loads(json.dumps(declaration))
    path = folder / "declaration.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != declaration:
            raise ValueError(
                "Frozen declaration differs; create a new explicitly versioned experiment"
            )
    else:
        write_json(path, declaration)
    return declaration


def prepare(folder, payload):
    members = {
        m["ts_code"]: m
        for m in payload["members"]
        if is_mainboard(m["ts_code"])
        and "ST" not in m["name"].upper()
        and "退" not in m["name"]
    }
    series = {k: [] for k in ENTRY_PROFILES}
    excluded = []
    audit = []
    frozen = [s for s in payload["series"] if s.code in members]
    for n, s in enumerate(frozen, 1):
        path = folder / "minutes" / f"{s.code}.pkl"
        if not path.exists():
            raise ValueError(
                f"Minute cache incomplete: {s.code}; finish/resume download first"
            )
        minute = pd.read_pickle(path)  # Local cache created by the downloader.
        minute["bar_end"] = pd.to_datetime(minute.bar_end)
        close_rows = minute[minute.bar_end.dt.hour.eq(15)].set_index("bar_end")
        daily_close = pd.Series(s.data["close"], index=s.times)
        overlap = close_rows.close.to_frame("minute").join(
            daily_close.rename("daily"), how="inner"
        )
        deviations = ((overlap.minute / overlap.daily - 1).abs() > 0.005).sum()
        audit.append(
            dict(
                code=s.code,
                minute_rows=len(minute),
                daily_rows=len(s.times),
                source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                close_deviations_over_half_pct=int(deviations),
                minute_days=minute.bar_end.dt.normalize().nunique(),
            )
        )
        if deviations > max(1, len(overlap) * 0.01):
            excluded.append(
                dict(code=s.code, issue="分钟与冻结原生日K收盘不一致超过1%日期")
            )
            continue
        results = {}
        for name, cfg in ENTRY_PROFILES.items():
            prepared, issue = prepare_swing(minute, s, cfg)
            if issue:
                excluded.append(dict(code=s.code, issue=issue))
                break
            results[name] = prepared
        if len(results) == len(ENTRY_PROFILES):
            for name, s2 in results.items():
                series[name].append(s2)
        if n % 100 == 0 or n == len(frozen):
            logger.info(
                "60F research prepare %s/%s excluded=%s", n, len(frozen), len(excluded)
            )
    if len(excluded) / max(1, len(members)) > 0.02:
        write_json(folder / "excluded.json", excluded)
        raise ValueError(
            "More than 2% of the declared universe lacks valid inputs; refuse biased result"
        )
    prepared = dict(series=series, excluded=excluded, universe_size=len(members))
    with (folder / "prepared.pkl").open("wb") as stream:
        pickle.dump(prepared, stream, pickle.HIGHEST_PROTOCOL)
    write_json(folder / "excluded.json", excluded)
    write_json(folder / "data_audit.json", audit)
    return prepared


def score(metrics):
    return metrics["total_return_pct"] / max(metrics["max_drawdown_pct"], 1)


def risk_eligible(result):
    m = result["metrics"]
    return (
        m["closed_trades"] >= LIMITS["min_closed_trades"]
        and m["max_drawdown_pct"] <= LIMITS["max_drawdown_pct"]
        and min((t["return_pct"] for t in result["trades"]), default=0)
        >= LIMITS["worst_trade_pct"]
    )


def qualified(result):
    m = result["metrics"]
    return (
        risk_eligible(result)
        and m["total_return_pct"] > 0
        and m["profit_factor"] is not None
        and m["profit_factor"] > LIMITS["min_profit_factor"]
    )


def run(folder, prepared, declaration):
    # The previously completed portfolio's exchange calendar is frozen input.
    root = Path(get_config().root_dir)
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

    def calculate(entry, exit_name, strategy, window, cfg=None):
        start, end = WINDOWS[window]
        stocks = window_series(prepared["series"][entry], start, end)
        timeline = days[
            (days >= start) & (days < pd.Timestamp(end) + pd.Timedelta(days=1))
        ]
        r = simulate(
            stocks,
            timeline,
            strategy,
            "1D",
            "fixed_snapshot",
            cfg or EXIT_PROFILES[exit_name],
        )
        r.update(
            run_id=str(uuid4()),
            suite_id=suite,
            strategy_id=strategy,
            strategy_version=VERSION,
            strategy_config=asdict(ENTRY_PROFILES[entry]),
            timeframe="1D",
            universe_mode="fixed_snapshot",
            start=start,
            end=end,
            diagnostics=dict(
                trend_timeframe="60F",
                entry_timeframe="1D",
                execution_schedule="after_close",
                board="mainboard",
                exit_profile=exit_name,
                entry_profile=entry,
                research_window=window,
                universe_size=prepared["universe_size"],
                usable_stocks=len(stocks),
                bar_count=sum(len(s.times) for s in stocks),
                excluded=prepared["excluded"],
                in_sample=True,
                warnings=declaration["limitations"],
            ),
        )
        r["metrics"]["worst_trade_return_pct"] = min(
            (t["return_pct"] for t in r["trades"]), default=None
        )
        return r

    candidates = []
    for entry in ENTRY_PROFILES:
        for strategy in STRATEGIES:
            for exit_name in EXIT_PROFILES:
                r = calculate(entry, exit_name, strategy, "development")
                key = f"{entry}__{strategy}__{exit_name}"
                write_result(folder / "development" / key, r)
                candidates.append((entry, exit_name, strategy, r))
                logger.info(
                    "Development %s/36 %s return=%.3f drawdown=%.3f trades=%s",
                    len(candidates),
                    key,
                    r["metrics"]["total_return_pct"],
                    r["metrics"]["max_drawdown_pct"],
                    r["metrics"]["closed_trades"],
                )
    eligible = [c for c in candidates if qualified(c[3])]
    risk_pool = [c for c in candidates if risk_eligible(c[3])]
    pool = eligible or risk_pool or candidates
    winner = max(pool, key=lambda c: (score(c[3]["metrics"]), c[:3]))
    entry, exit_name, strategy, train = winner
    selection = dict(
        entry=entry,
        exit_profile=exit_name,
        strategy_id=strategy,
        qualified=qualified(train),
        selected_on="development only",
        training_metrics=train["metrics"],
        target=LIMITS,
        candidate_count=len(candidates),
        qualifying_count=len(eligible),
        ranking=[
            dict(
                entry=a,
                exit_profile=b,
                strategy_id=c,
                qualified=qualified(r),
                metrics=r["metrics"],
                worst_trade_pct=min((t["return_pct"] for t in r["trades"]), default=0),
            )
            for a, b, c, r in sorted(
                candidates, key=lambda c: score(c[3]["metrics"]), reverse=True
            )
        ],
    )
    write_json(folder / "selection.json", selection)
    reports = {"development": train}
    for window in ["validation", "time_holdout", "full"]:
        r = calculate(entry, exit_name, strategy, window)
        r["diagnostics"]["training_qualified"] = selection["qualified"]
        r["diagnostics"]["research_selected"] = window == "full"
        write_result(folder / "selected" / window, r)
        reports[window] = r
    stressed = calculate(
        entry,
        exit_name,
        strategy,
        "full",
        replace(EXIT_PROFILES[exit_name], slippage=0.001),
    )
    stressed["diagnostics"].update(
        training_qualified=selection["qualified"], research_selected=False
    )
    write_result(folder / "selected" / "cost_stress", stressed)
    summary = dict(
        selection={k: v for k, v in selection.items() if k != "ranking"},
        windows={k: v["metrics"] for k, v in reports.items()},
        cost_stress=stressed["metrics"],
        target_met=bool(
            selection["qualified"]
            and reports["full"]["metrics"]["total_return_pct"] >= 5
            and reports["full"]["metrics"]["max_drawdown_pct"] <= 8
            and min((t["return_pct"] for t in reports["full"]["trades"]), default=0)
            >= -8
            and stressed["metrics"]["total_return_pct"] > 0
        ),
        independent_validation=False,
    )
    write_json(folder / "comparison.json", summary)
    write_research_summary(folder)
    print(json.dumps(summary, ensure_ascii=False))


def write_research_summary(folder):
    summary = json.loads((folder / "comparison.json").read_text(encoding="utf-8"))
    selected = summary["selection"]
    lines = [
        "# 主板60F策略参数研究",
        "",
        "区间：2026-01-01—2026-11-12；原五指数固定名单中的2333只主板非ST，非全部上市主板。",
        "**2026已知样例与当前名单参与设计，存在样本内、成分前视和幸存者偏差；不是独立样本外业绩。**",
        "",
        f"目标5%及风险检查：{'通过本次历史研究门槛' if summary['target_met'] else '未通过，不应据此上线交易'}。开发段资格：{selected['qualified']}。",
        "",
        f"冻结选择：{selected['entry']} / {selected['strategy_id']} / {selected['exit_profile']}。",
        "36组只在1—6月选择。7—8月、9—11月与全年固定参数检验；分段各自空仓起算，收益不可相加。",
        "",
        "| 区间 | 净收益 | 最大回撤 | 平仓数 | 胜率 | 最差单笔 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, m in {
        **summary["windows"],
        "double_slippage": summary["cost_stress"],
    }.items():
        win = m["win_rate_pct"]
        worst = m.get("worst_trade_return_pct")
        lines.append(
            f"| {name} | {m['total_return_pct']:.2f}% | {m['max_drawdown_pct']:.2f}% | {m['closed_trades']} | {win:.2f}% | {worst:.2f}% |"
            if win is not None and worst is not None
            else f"| {name} | {m['total_return_pct']:.2f}% | {m['max_drawdown_pct']:.2f}% | {m['closed_trades']} | — | — |"
        )
    lines += [
        "",
        "全部形态和MACD/M8/13/21均来自60F，日K仅为原生执行价格及流动性。低点在后两根确认后才能使用；盘后汇总当日完整四根，次日开盘尝试入场。",
        "止盈止损按收盘检查，次日开盘执行；不假设盘中触价成交。单股最多10%、最多10只，单笔计划账户风险0.35%，开盘风险距离最多4%。费用、滑点、真实涨跌停、T+1与现金约束保留。",
        "4%与0.35%不是实际损失承诺，隔夜跳空及跌停可能超出。开发段要求至少30笔、正收益、PF>1、最大回撤≤8%、最差单笔≥−8%；全年目标还要求收益≥5%、最大回撤≤8%、最差单笔≥−8%、双倍滑点仍盈利。",
        "",
        "参数与全部候选：declaration.json、selection.json、development/；固定检验成交/净值：selected/；数据审计：data_audit.json、excluded.json、cache_manifest.json。",
        "",
        "研究定义参考：[MACD底背离](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/macd-moving-average-convergence-divergence-oscillator)。定义不代表收益已获证明。",
    ]
    (folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--publish-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    folder = root / "output/quant/mainboard_60f_v8"
    folder.mkdir(parents=True, exist_ok=True)
    source = root / "output/quant/reversal_v6_1/prepared.pkl"
    declaration = preregister(folder, source)
    if args.report_only or args.publish_only:
        write_research_summary(folder)
        if args.publish_only:
            from jobs.backtest_quant import _database_unit
            from storage.repositories.backtest import publish, annotate_diagnostics
            from storage.schema.quant import ensure_quant_schema

            ensure_quant_schema()
            selected = json.loads(
                (folder / "selection.json").read_text(encoding="utf-8")
            )
            for window in ["full", "cost_stress"]:
                (path,) = (folder / "selected" / window).glob("*.json")
                result = json.loads(path.read_text(encoding="utf-8"))
                result["diagnostics"].update(
                    training_qualified=selected["qualified"],
                    research_selected=window == "full",
                )
                write_json(path, result)
                _database_unit(lambda: publish(result))
                _database_unit(
                    lambda: annotate_diagnostics(
                        result["run_id"], result["diagnostics"]
                    )
                )
            print(
                "Published selected full-period research and cost stress; no default trading activation."
            )
        return
    if args.declare_only:
        print("Declared 36 candidates; select only on development, target 5%, MDD 8%.")
        return
    if args.resume:
        with (folder / "prepared.pkl").open("rb") as stream:
            prepared = pickle.load(stream)
    else:
        with source.open("rb") as stream:
            prepared = prepare(folder, pickle.load(stream))
    if not args.prepare_only:
        run(folder, prepared, declaration)


if __name__ == "__main__":
    main()
