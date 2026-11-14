"""Fixed v8/v9 entry and exit ablations; no parameter search or winner picking.

2026 was already inspected. These segments are diagnostic, not unseen tests.
Old artifacts remain intact. All new outputs live under one versioned folder.
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
    qualified,
    write_json,
)
from skills.quant.engine import simulate
from skills.quant.models import StructureExitConfig
from skills.quant.prepare import MarketSeries, prepare_structure
from skills.quant.report import write_result
from skills.trend.structure import StructureConfig, VERSION
from skills.trend.swing import VERSION as OLD_VERSION

logger = get_logger(__name__)
ENTRY = StructureConfig()
CARRY = StructureExitConfig(risk_budget_pct=0.35, max_entry_risk_pct=4)
OLD_EXIT = EXIT_PROFILES["scheduled_reversal"]
VARIANTS = {
    "old_entry_old_exit": ("old", "swing_60f_v8", OLD_EXIT),
    "new_entry_old_exit": ("new", "swing_structure_v9", OLD_EXIT),
    "old_entry_structure_exit": ("old", "swing_60f_v8", CARRY),
    "new_entry_structure_exit": ("new", "swing_structure_v9", CARRY),
    "new_reversal_structure_exit": ("new", "swing_structure_reversal_v9", CARRY),
    "new_pullback_structure_exit": ("new", "swing_structure_pullback_v9", CARRY),
    "new_entry_precommitted_old_exit": (
        "new",
        "swing_structure_v9",
        replace(OLD_EXIT, close_only=False),
    ),
}
PRIMARY = "new_entry_structure_exit"


def gate_result(result, primary, passed):
    """Keep rejected research reviewable without making it the default view."""
    result["diagnostics"].update(
        primary_candidate=primary,
        eligible_for_default=bool(primary and passed),
        research_selected=bool(primary and passed),
        research_status="passed_history" if primary and passed else "rejected",
    )


def declare(folder, source, root):
    code_files = [
        "skills/trend/structure.py",
        "skills/quant/structure_exit.py",
        "skills/quant/prepare.py",
        "skills/quant/engine.py",
    ]
    value = json.loads(
        json.dumps(
            dict(
                version=VERSION,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                code_sha256={
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in code_files
                },
                entry=asdict(ENTRY),
                old_entry=asdict(ENTRY_PROFILES["balanced"]),
                variants={
                    name: dict(entry=e, strategy=s, execution=asdict(c))
                    for name, (e, s, c) in VARIANTS.items()
                },
                primary=PRIMARY,
                windows=WINDOWS,
                selection="No optimization or winner selection; fixed primary and explanatory controls.",
                limitations=[
                    "2026 data and examples were already inspected: all results are in-sample research.",
                    "2333 current non-ST mainboard constituents of the fixed five-index union, with survivorship/lookahead bias.",
                    "Daily bars are execution prices only. All entry and support evidence comes from completed native 60F.",
                    "After-close thresholds execute next session; precommitted control requires broker conditions and is separate.",
                    "Planned distance 4% and account risk .35% cannot guarantee realized loss limits.",
                ],
            )
        )
    )
    path = folder / "declaration.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
        raise ValueError(
            "Frozen entry/exit/code declaration changed; use a new explicit experiment"
        )
    if not path.exists():
        write_json(path, value)
    return value


def prepare(folder, source):
    with source.open("rb") as stream:
        base = pickle.load(stream)
    series = {"old": [], "new": []}
    audit = []
    for n, old in enumerate(base["series"]["balanced"], 1):
        path = source.parent / "minutes" / f"{old.code}.pkl"
        stock, issue = prepare_structure(pd.read_pickle(path), old, ENTRY)
        if issue:
            raise ValueError(f"{old.code}: {issue}")
        data = dict(old.data)
        for key in ["structural_support", "support_origin", "structure_failed"]:
            data[key] = stock.data[key]
        series["old"].append(
            MarketSeries(
                old.code, old.name, old.observed_at, old.times, data, old.signals
            )
        )
        series["new"].append(stock)
        audit.append(
            dict(
                code=old.code,
                old_signals=len(old.signals),
                new_signals=len(stock.signals),
                reversal=sum(s["family"] == "base_rebound" for s in stock.signals),
                pullback=sum(s["family"] == "trend_pullback" for s in stock.signals),
            )
        )
        if n % 100 == 0 or n == len(base["series"]["balanced"]):
            logger.info(
                "Ordered 60F prepare %s/%s signals=%s",
                n,
                len(base["series"]["balanced"]),
                sum(r["new_signals"] for r in audit),
            )
    result = dict(
        series=series, universe_size=base["universe_size"], excluded=base["excluded"]
    )
    with (folder / "prepared.pkl").open("wb") as stream:
        pickle.dump(result, stream, pickle.HIGHEST_PROTOCOL)
    pd.DataFrame(audit).to_csv(
        folder / "signal_audit.csv", index=False, encoding="utf-8-sig"
    )
    return result


def run(folder, payload, declared, root):
    reference = json.loads(
        (
            root
            / "output/quant/reversal_v6_1/full/1D_mtf_reversal_v6_fixed_snapshot.json"
        ).read_text(encoding="utf-8")
    )
    days = pd.DatetimeIndex(
        [pd.Timestamp(p["date"]) + pd.Timedelta(hours=15) for p in reference["equity"]]
    )
    suite = str(uuid4())
    results = {}
    for name, (entry, strategy, cfg) in VARIANTS.items():
        results[name] = {}
        for window, (start, end) in WINDOWS.items():
            stocks = window_series(payload["series"][entry], start, end)
            clock = days[
                (days >= start) & (days < pd.Timestamp(end) + pd.Timedelta(days=1))
            ]
            r = simulate(stocks, clock, strategy, "1D", "fixed_snapshot", cfg)
            r.update(
                run_id=str(uuid4()),
                suite_id=suite,
                strategy_id=strategy,
                strategy_version=VERSION if entry == "new" else OLD_VERSION,
                strategy_config=asdict(
                    ENTRY if entry == "new" else ENTRY_PROFILES["balanced"]
                ),
                timeframe="1D",
                universe_mode="fixed_snapshot",
                start=start,
                end=end,
                diagnostics=dict(
                    trend_timeframe="60F",
                    entry_timeframe="1D",
                    board="mainboard",
                    execution_schedule="after_close"
                    if cfg.close_only
                    else "precommitted",
                    entry_profile="ordered_structure"
                    if entry == "new"
                    else "balanced_v8_control",
                    exit_profile="structural_carry"
                    if cfg.exit_policy == "structure"
                    else "reversal_staged",
                    research_window=window,
                    research_variant=name,
                    research_kind="ordered_ablation",
                    research_selected=name == PRIMARY and window == "full",
                    universe_size=payload["universe_size"],
                    usable_stocks=len(stocks),
                    bar_count=sum(len(s.times) for s in stocks),
                    in_sample=True,
                    warnings=declared["limitations"],
                ),
            )
            r["metrics"]["worst_trade_return_pct"] = min(
                (t["return_pct"] for t in r["trades"]), default=None
            )
            r["diagnostics"]["training_qualified"] = (
                qualified(r)
                if window == "development"
                else results[name]["development"]["diagnostics"]["training_qualified"]
            )
            write_result(folder / name / window, r)
            results[name][window] = r
        logger.info(
            "Ordered control %s full_return=%.3f drawdown=%.3f trades=%s",
            name,
            results[name]["full"]["metrics"]["total_return_pct"],
            results[name]["full"]["metrics"]["max_drawdown_pct"],
            results[name]["full"]["metrics"]["closed_trades"],
        )
    primary = results[PRIMARY]["full"]
    stocks = window_series(payload["series"]["new"], *WINDOWS["full"])
    stress = simulate(
        stocks,
        days,
        "swing_structure_v9",
        "1D",
        "fixed_snapshot",
        replace(CARRY, slippage=0.001),
    )
    stress.update(
        {k: v for k, v in primary.items() if k not in stress and k != "trades"}
    )
    stress["run_id"] = str(uuid4())
    stress["diagnostics"] = {
        **primary["diagnostics"],
        "research_selected": False,
        "research_variant": "cost_stress",
    }
    stress["metrics"]["worst_trade_return_pct"] = min(
        (t["return_pct"] for t in stress["trades"]), default=None
    )
    write_result(folder / "cost_stress", stress)
    m = primary["metrics"]
    target = bool(
        m["total_return_pct"] >= 5
        and m["max_drawdown_pct"] <= 8
        and (m["worst_trade_return_pct"] or 0) >= -8
        and stress["metrics"]["total_return_pct"] > 0
    )
    summary = dict(
        primary=PRIMARY,
        historical_target_met=target,
        development_qualified=primary["diagnostics"]["training_qualified"],
        variants={
            name: {w: r["metrics"] for w, r in windows.items()}
            for name, windows in results.items()
        },
        cost_stress=stress["metrics"],
        selection="fixed primary, no winner selection",
        independent_validation=False,
    )
    passed = bool(target and primary["diagnostics"]["training_qualified"])
    summary["research_passed"] = passed
    for name, windows in results.items():
        gate_result(windows["full"], name == PRIMARY, passed)
        write_result(folder / name / "full", windows["full"])
    gate_result(stress, False, False)
    write_result(folder / "cost_stress", stress)
    write_json(folder / "comparison.json", summary)
    lines = [
        "# 60F有序结构固定对照",
        "",
        "**2026样本内研究；全部规则在本轮收益计算前冻结，不挑选全年赢家。**",
        f"固定主方案：{PRIMARY}；历史收益/风险目标通过：{target}；开发段资格：{summary['development_qualified']}。",
        "",
        "| 固定方案 | 累计收益 | 最大回撤 | 平仓数 | 胜率 | 最差单笔 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, windows in results.items():
        x = windows["full"]["metrics"]
        win = x["win_rate_pct"]
        worst = x["worst_trade_return_pct"]
        lines.append(
            f"| {name} | {x['total_return_pct']:.2f}% | {x['max_drawdown_pct']:.2f}% | {x['closed_trades']} | {win:.2f}% | {worst:.2f}% |"
            if win is not None
            else f"| {name} | 无交易 | — | 0 | — | — |"
        )
    lines += [
        "",
        "首四行分离入场与退出的贡献；两个单方向行用于归因；最后条件单行仅为执行方式对照，不与盘后手动处理混同。",
        "入场：有序推进至少5%且4ATR；回撤不超过前推进的一半、低点不破结构、下降阶段相对同钟点量能收缩后再转强。底部需两个有反弹间隔的低点、DIF改善与测试量能衰减。低点后两根确认，信号不回填。",
        "结构持有：原止损不放宽；收盘同时达到1.5R及3%才保护成本，并仅用入场后形成且已确认的60F低点抬升保护；连续60F跌破M21且M13向下可判结构破坏。盘后检查、次日执行，不固定2R或3R截断。",
        f"主方案双倍滑点收益{stress['metrics']['total_return_pct']:.2f}%；风险预算0.35%、开仓止损距离4%，实际亏损仍可能因跳空/跌停突破。",
        "2333只当前非ST主板、原五指数固定池；并非历史无偏名单。2026已知案例和之前研究都已看过，各分段不是独立样本外。完整参数见declaration.json，全部分段与逐笔见方案子目录，信号覆盖见signal_audit.csv。",
    ]
    (folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "variants"}, ensure_ascii=False
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--publish-only", action="store_true")
    parser.add_argument("--publish-variant", choices=[*VARIANTS, "cost_stress"])
    args = parser.parse_args()
    if args.publish_variant and not args.publish_only:
        parser.error("--publish-variant requires --publish-only")
    root = Path(get_config().root_dir)
    folder = root / "output/quant/ordered_60f_v9"
    folder.mkdir(parents=True, exist_ok=True)
    source = root / "output/quant/mainboard_60f_v8/prepared.pkl"
    declared = declare(folder, source, root)
    if args.declare_only:
        print("Frozen 7 fixed controls; no parameter search or winner selection.")
        return
    if args.publish_only:
        from jobs.backtest_quant import _database_unit
        from storage.repositories.backtest import publish, annotate_diagnostics
        from storage.schema.quant import ensure_quant_schema

        ensure_quant_schema()
        comparison = json.loads(
            (folder / "comparison.json").read_text(encoding="utf-8")
        )
        passed = bool(
            comparison["historical_target_met"] and comparison["development_qualified"]
        )
        comparison["research_passed"] = passed
        write_json(folder / "comparison.json", comparison)
        names = (
            [args.publish_variant]
            if args.publish_variant
            else [*VARIANTS, "cost_stress"]
        )
        for name in names:
            directory = (
                folder / name if name == "cost_stress" else folder / name / "full"
            )
            (path,) = directory.glob("*.json")
            r = json.loads(path.read_text(encoding="utf-8"))
            logger.info("Publishing ordered result %s", name)
            gate_result(r, name == PRIMARY, passed)
            write_json(path, r)
            _database_unit(lambda: publish(r))
            _database_unit(lambda: annotate_diagnostics(r["run_id"], r["diagnostics"]))
            logger.info("Published ordered result %s", name)
        print(f"Published {len(names)} complete ordered results; no automatic trading.")
        return
    if args.resume:
        with (folder / "prepared.pkl").open("rb") as stream:
            payload = pickle.load(stream)
    else:
        payload = prepare(folder, source)
    run(folder, payload, declared, root)


if __name__ == "__main__":
    main()
