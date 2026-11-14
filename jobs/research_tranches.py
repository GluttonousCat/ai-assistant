"""Fixed 442/532 campaign comparisons on frozen v8/v9 entries; no winner search."""

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
from jobs.research_mainboard_60f import ENTRY_PROFILES, EXIT_PROFILES, write_json
from skills.quant.engine import simulate as legacy_simulate
from skills.quant.prepare import MarketSeries
from skills.quant.report import write_result
from skills.quant.tranches import TrancheConfig
from skills.quant.tranche_engine import simulate
from skills.quant.tranche_prepare import attach_confirmations
from skills.trend.structure import StructureConfig, VERSION as NEW_VERSION
from skills.trend.swing import VERSION as OLD_VERSION

logger = get_logger(__name__)
P442 = (0.4, 0.4, 0.2)
P532 = (0.5, 0.3, 0.2)


def plan(entry, exits):
    return TrancheConfig(
        entry_weights=entry,
        exit_weights=exits,
        add_at_r=(0.5, 1.0) if len(entry) == 3 else (),
        take_profit_pct=(5.0, 8.0) if len(exits) == 3 else (),
    )


PLANS = {
    "full_entry_full_exit": plan((1.0,), (1.0,)),
    "entry_442_only": plan(P442, (1.0,)),
    "exit_442_only": plan((1.0,), P442),
    "entry_exit_442": plan(P442, P442),
    "entry_exit_532": plan(P532, P532),
}
PRIMARY = "entry_exit_442"


def declaration(folder, source, root):
    files = [
        "skills/quant/tranches.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranche_prepare.py",
    ]
    value = json.loads(
        json.dumps(
            dict(
                version="tranche-study-v10",
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                code_sha256={
                    f: hashlib.sha256((root / f).read_bytes()).hexdigest()
                    for f in files
                },
                plans={k: asdict(v) for k, v in PLANS.items()},
                entries=["old", "new"],
                windows=WINDOWS,
                primary=PRIMARY,
                legacy_control=asdict(EXIT_PROFILES["scheduled_reversal"]),
                selection="Fixed 442 primary; controls only, no winner picking.",
                semantics="442/532 are fractions of the stock plan. Adds at .5R/1R with completed-60F evidence, only at rising fill prices. Sell accumulated position at +5%/+8%, retain last tail. Hard exits override all tranches.",
                limitations=[
                    "All 2026 segments/examples were inspected; in-sample only.",
                    "2333 current non-ST mainboard stocks of a fixed five-index union; survivor/membership bias.",
                    "Next-open fills, T+1 per acquisition, actual limits and fees. Planned 4% distance/.35% account risk may be exceeded by gaps.",
                    "Factor-share approximation, not corporate-action cash accounting. Lot/cash/risk caps can leave a plan partly filled.",
                    "One completed campaign counts once; open partial realized profits are accounted separately.",
                ],
            )
        )
    )
    path = folder / "declaration.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
        raise ValueError("Frozen staged experiment changed")
    if not path.exists():
        write_json(path, value)
    return value


def prepare(folder, source):
    with source.open("rb") as stream:
        base = pickle.load(stream)
    old = {s.code: s for s in base["series"]["old"]}
    series = {"old": [], "new": []}
    minute_root = source.parent.parent / "mainboard_60f_v8/minutes"
    for n, s in enumerate(base["series"]["new"], 1):
        stock = attach_confirmations(pd.read_pickle(minute_root / f"{s.code}.pkl"), s)
        series["new"].append(stock)
        prior = old[s.code]
        data = dict(prior.data)
        for k in ["scale_in_ready", "tranche_trend_failed"]:
            data[k] = stock.data[k]
        series["old"].append(
            MarketSeries(
                prior.code,
                prior.name,
                prior.observed_at,
                prior.times,
                data,
                prior.signals,
            )
        )
        if n % 250 == 0 or n == len(old):
            logger.info("Tranche inputs %s/%s", n, len(old))
    payload = dict(
        series=series, universe_size=base["universe_size"], excluded=base["excluded"]
    )
    with (folder / "prepared.pkl").open("wb") as stream:
        pickle.dump(payload, stream, pickle.HIGHEST_PROTOCOL)
    return payload


def gate(result, stress):
    m = result["metrics"]
    return bool(
        m["total_return_pct"] >= 5
        and m["max_drawdown_pct"] <= 8
        and m["closed_trades"] >= 30
        and min((t["return_pct"] for t in result["trades"]), default=0) >= -8
        and stress["metrics"]["total_return_pct"] > 0
    )


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
    summary = {}
    for entry in ["old", "new"]:
        for name in ["legacy_single", *PLANS]:
            key = f"{entry}__{name}"
            results[key] = {}
            summary[key] = {}
            for window, (start, end) in WINDOWS.items():
                stocks = window_series(payload["series"][entry], start, end)
                clock = days[
                    (days >= start) & (days < pd.Timestamp(end) + pd.Timedelta(days=1))
                ]
                strategy = "swing_60f_v8" if entry == "old" else "swing_structure_v9"
                r = (
                    legacy_simulate(
                        stocks,
                        clock,
                        strategy,
                        "1D",
                        "fixed_snapshot",
                        EXIT_PROFILES["scheduled_reversal"],
                    )
                    if name == "legacy_single"
                    else simulate(stocks, clock, strategy, PLANS[name])
                )
                r.update(
                    run_id=str(uuid4()),
                    suite_id=suite,
                    strategy_id=strategy,
                    strategy_version=OLD_VERSION if entry == "old" else NEW_VERSION,
                    strategy_config=asdict(
                        ENTRY_PROFILES["balanced"]
                        if entry == "old"
                        else StructureConfig()
                    ),
                    timeframe="1D",
                    universe_mode="fixed_snapshot",
                    start=start,
                    end=end,
                    diagnostics=dict(
                        trend_timeframe="60F",
                        entry_timeframe="1D",
                        execution_schedule="after_close",
                        research_kind="tranche_campaign",
                        research_variant=key,
                        exit_profile=name,
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
                write_result(folder / key / window, r)
                results[key][window] = r
                summary[key][window] = r["metrics"]
            m = results[key]["full"]["metrics"]
            logger.info(
                "Tranche control %s return=%.3f drawdown=%.3f campaigns=%s",
                key,
                m["total_return_pct"],
                m["max_drawdown_pct"],
                m["closed_trades"],
            )
    stress = {}
    for entry in ["old", "new"]:
        original = results[f"{entry}__{PRIMARY}"]["full"]
        p = PLANS[PRIMARY]
        r = simulate(
            window_series(payload["series"][entry], *WINDOWS["full"]),
            days,
            original["strategy_id"],
            replace(p, base=replace(p.base, slippage=0.001)),
        )
        r.update({k: v for k, v in original.items() if k not in r})
        r["run_id"] = str(uuid4())
        r["diagnostics"] = {
            **original["diagnostics"],
            "research_variant": f"{entry}__{PRIMARY}__cost_stress",
        }
        write_result(folder / f"{entry}__{PRIMARY}__cost_stress", r)
        stress[entry] = r
    passed = {
        e: gate(results[f"{e}__{PRIMARY}"]["full"], stress[e]) for e in ["old", "new"]
    }
    write_json(
        folder / "comparison.json",
        dict(
            primary=PRIMARY,
            variants=summary,
            cost_stress={k: v["metrics"] for k, v in stress.items()},
            historical_target_met=passed,
            independent_validation=False,
        ),
    )
    lines = [
        "# 442/532分批进出场固定对照",
        "",
        "**2026样本内研究，不挑选全年赢家；每个完整持仓周期只算一笔交易。**",
        "",
        "| 入场/仓位方案 | 累计收益 | 回撤 | 完成周期 | 胜率 | 最差周期 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, windows in summary.items():
        m = windows["full"]
        win = m["win_rate_pct"]
        worst = m["worst_trade_return_pct"]
        lines.append(
            f"| {key} | {m['total_return_pct']:.2f}% | {m['max_drawdown_pct']:.2f}% | {m['closed_trades']} | {win:.2f}% | {worst:.2f}% |"
            if win is not None
            else f"| {key} | 无成交 | — | 0 | — | — |"
        )
    lines += [
        "",
        "old=冻结v8 balanced双方向；new=冻结v9有序结构。legacy_single是旧分段退出；其余为同一分批引擎的单买单卖、只分批买、只分批卖、442双向、532双向对照。",
        "442/532分配单股计划资金，非全账户仓位。单股最多10%、最多10只，风险预算0.35%、每次入场止损距离最多4%；受整手/现金/实时仓位/剩余风险限制，可只完成一笔或两笔。盈利0.5R/1R且60F确认后才增仓，实际成交价须高于此前买价，禁止下跌摊薄成本。",
        "实际建仓单位为分批卖出基数；相对冻结平均成本收盘盈利5%/8%后，下一交易日分别卖出前两笔，尾仓按趋势/止损/20日超时退出。减仓意图出现后锁定增仓，跳空跌破止损覆盖分批计划，全退T+1可卖部分。盈利5%后保护剩余成本并按60F M21留0.5ATR上移保护。",
        "每次买卖的数量/费用/金额/原因在fills中保存。未结束周期的部分已实现收益单列，不丢失或与剩余浮盈重复。净值=初始资金+已结束周期盈亏+未结束周期已实现部分+剩余浮盈。真实限价、T+1及费用保留，因子份额为近似，不还原逐笔权益台账。",
        f"固定442历史目标检查：{passed}；全部方案限研究，不自动替换默认。参数、代码/数据指纹见declaration.json，完整分段/逐笔/净值见方案目录。",
    ]
    (folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(dict(historical_target_met=passed), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ["declare-only", "resume", "publish-only"]:
        parser.add_argument("--" + option, action="store_true")
    parser.add_argument("--publish-variant")
    parser.add_argument("--trade-page-size", type=int, default=25)
    args = parser.parse_args()
    if not 1 <= args.trade_page_size <= 500:
        parser.error("--trade-page-size must be 1..500")
    if args.publish_variant and not args.publish_only:
        parser.error("--publish-variant requires --publish-only")
    root = Path(get_config().root_dir)
    folder = root / "output/quant/tranches_v10"
    folder.mkdir(parents=True, exist_ok=True)
    source = root / "output/quant/ordered_60f_v9/prepared.pkl"
    declared = declaration(folder, source, root)
    if args.declare_only:
        print("Frozen 442/532 plans and fixed controls, no winner selection.")
        return
    if args.publish_only:
        from jobs.backtest_quant import _database_unit
        from storage.repositories.backtest import publish
        from storage.schema.quant import ensure_quant_schema

        files = {
            f"{e}__{n}": next((folder / f"{e}__{n}" / "full").glob("*.json"))
            for e in ["old", "new"]
            for n in ["legacy_single", *PLANS]
        }
        files.update(
            {
                f"{e}__{PRIMARY}__cost_stress": next(
                    (folder / f"{e}__{PRIMARY}__cost_stress").glob("*.json")
                )
                for e in ["old", "new"]
            }
        )
        if args.publish_variant and args.publish_variant not in files:
            parser.error("unknown fixed variant")
        ensure_quant_schema()
        for name in [args.publish_variant] if args.publish_variant else files:
            logger.info("Publishing tranche result %s", name)
            r = json.loads(files[name].read_text(encoding="utf-8"))
            _database_unit(lambda: publish(r, trade_page_size=args.trade_page_size))
            logger.info("Published tranche result %s", name)
        print("Published complete staged campaign reports; defaults unchanged.")
        return
    if args.resume:
        with (folder / "prepared.pkl").open("rb") as stream:
            payload = pickle.load(stream)
    else:
        payload = prepare(folder, source)
    run(folder, payload, declared, root)


if __name__ == "__main__":
    main()
