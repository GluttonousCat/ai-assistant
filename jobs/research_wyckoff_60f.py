"""V31 fixed price/volume primary and management controls, no winner search."""

import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path
import pickle
from uuid import uuid4

import numpy as np
import pandas as pd

from core.config import get_config
from core.logger import get_logger
from jobs.backtest_mtf import WINDOWS, window_series
from jobs.research_mainboard_60f import write_json
from jobs.research_quality_60f import (
    BASELINE,
    STRESS,
    ORIGINAL,
    RANKED,
    configuration,
    digest,
    freeze,
    read_report,
)
from skills.quant.candidate_quality import attach_signal_quality
from skills.quant.market_context import MarketPolicy
from skills.quant.report import write_result, with_benchmarks
from skills.quant.support_protection import SupportProtection
from skills.quant.tranche_engine import simulate
from skills.quant.wyckoff import prepare_wyckoff
from skills.trend.wyckoff import VERSION, WyckoffConfig

logger = get_logger(__name__)
FOLDER = "wyckoff_price_volume_v33"
STUDY_VERSION = "wyckoff-fixed-comparison-v33"
CONFIG = WyckoffConfig()
PROTECTION = SupportProtection(0.5, 0.35)
CASES = ("baseline", "volume_management", "price_volume_primary")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    cache_args = parser.add_mutually_exclusive_group()
    cache_args.add_argument("--resume", action="store_true")
    cache_args.add_argument("--prepared-from", type=Path)
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    folder = root / "output/quant" / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    baseline_path, baseline = read_report(root / BASELINE)
    stress_path, prior_stress = read_report(root / STRESS)
    with (root / ORIGINAL).open("rb") as stream:
        original = pickle.load(stream)["series"]
    with (root / RANKED).open("rb") as stream:
        ranked = pickle.load(stream)["series"]
    legacy = attach_signal_quality(ranked, original)
    files = {
        s.code: root / "output/quant/mainboard_60f_v8/minutes" / f"{s.code}.pkl"
        for s in original
    }
    sources = [
        "jobs/research_wyckoff_60f.py",
        "skills/trend/wyckoff.py",
        "skills/quant/wyckoff.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranches.py",
        "skills/quant/models.py",
        "skills/quant/prepare.py",
        "skills/quant/registry.py",
        "skills/quant/support_protection.py",
        "skills/quant/stop_orders.py",
        "skills/quant/candidate_quality.py",
        "skills/quant/market_context.py",
        "skills/quant/report.py",
        "skills/quant/metrics.py",
        "jobs/research_quality_60f.py",
        "jobs/backtest_mtf.py",
    ]
    inputs = {
        str(Path(p).relative_to(root) if Path(p).is_absolute() else p): digest(root / p)
        for p in [ORIGINAL, RANKED, baseline_path, stress_path, *files.values()]
    }
    prepared_from = args.prepared_from.resolve() if args.prepared_from else None
    if prepared_from:
        if not prepared_from.is_relative_to((root / "output/quant").resolve()):
            raise ValueError("仅复用本地研究目录内的可信冻结缓存")
        previous = json.loads(
            (prepared_from.parent / "declaration.json").read_text(encoding="utf-8")
        )
        if (
            previous["config"] != asdict(CONFIG)
            or previous["protection"] != asdict(PROTECTION)
            or previous["cases"] != list(CASES)
        ):
            raise ValueError("缓存策略参数不一致")
        for rel in ("skills/trend/wyckoff.py", "skills/quant/wyckoff.py"):
            if previous["code_sha256"][rel] != digest(root / rel):
                raise ValueError("缓存量价算法已变更")
        if any(inputs.get(k) != v for k, v in previous["input_sha256"].items()):
            raise ValueError("缓存行情输入已变更")
        inputs[str(prepared_from.relative_to(root))] = digest(prepared_from)
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=STUDY_VERSION,
            strategy_version=VERSION,
            config=asdict(CONFIG),
            protection=asdict(PROTECTION),
            cases=CASES,
            primary="price_volume_primary",
            windows=WINDOWS,
            input_sha256=inputs,
            code_sha256={p: digest(root / p) for p in sources},
            selection="Fixed primary plus old-entry/new-management and v21 controls. All three replay every declared window under both costs. No parameter search or full-year winner selection.",
            target="Full primary funded net>=8%, both costs MDD<=8%,worst>=-8%,>=30 cycles and stressed net>0. Failed experiments do not become defaults.",
            sources=[
                "https://chartschool.stockcharts.com/table-of-contents/market-analysis/wyckoff-analysis-articles/the-wyckoff-method-a-tutorial"
            ],
            warnings=[
                "Price/volume proxies, NOT a complete Wyckoff phase classifier or proof of institutional accumulation/distribution. No point-and-figure cause target is implemented.",
                "Pure primary uses no price-MA/MACD entry, add or exit predicate, no old relative-return model and no index-MA gate. ATR normalizes spread only. Legacy controls keep their original entries explicitly.",
                "Demand: spring plus lower-volume/narrower test and later recovery, or SOS then contracted-volume/spread pullback with renewed demand. Support becomes known on recovery, never backdated.",
                "Support formed after actual entry can move up after.5R; adds require renewed price/volume demand. High-context effort without progress pauses adds; subsequent weakness reduces one planned tranche. Support failure exits remaining inventory.",
                "Volume is inverse-scaled to the frozen factor-share basis to limit mechanical split jumps. Dividend factors and the cash corporate-action ledger remain approximations; raw execution prices are untouched.",
                "Original2333 current mainboard non-ST fixed-index union and all2026 have been inspected; current membership/survivorship bias remains. This is NOT independent out-of-sample evidence or a live recommendation.",
                "Primary native quality0-100>=72 differs from v8 native0-120 and frontend V7 grade. Scores are not win probabilities; old entries are not relabeled as volume-only.",
                "Daily bars only provide execution/liquidity/valuation; decisions use completed60F, next legal open,532,2.5% actual fill risk,.35% account risk,10% stock cap, real limits/T+1/fees. No leverage, broker orders, external fetching or default changes.",
            ],
        ),
    )
    for rel in sources:
        dest = folder / "source" / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((root / rel).read_bytes())
    if args.declare_only:
        print("Declared fixed price/volume primary and two controls.")
        return
    cache = folder / "prepared.pkl"
    if args.resume or prepared_from:
        with (prepared_from or cache).open("rb") as stream:
            variants = pickle.load(stream)
    else:
        legacy_by_code = {s.code: s for s in legacy}
        primary = []
        management = []
        for n, stock in enumerate(original, 1):
            prepared = prepare_wyckoff(pd.read_pickle(files[stock.code]), stock, CONFIG)
            primary.append(prepared)
            management.append(
                replace(prepared, signals=legacy_by_code[stock.code].signals)
            )
            if n % 250 == 0 or n == len(original):
                logger.info("Price/volume preparation %s/%s", n, len(original))
        variants = dict(
            baseline=legacy, volume_management=management, price_volume_primary=primary
        )
        with cache.open("wb") as stream:
            pickle.dump(variants, stream, pickle.HIGHEST_PROTOCOL)
        write_json(
            folder / "signal_audit.json",
            {
                name: dict(
                    stocks=len(stocks),
                    signals=sum(len(s.signals) for s in stocks),
                    reversals=sum(
                        s["family"] == "base_rebound"
                        for stock in stocks
                        for s in stock.signals
                    ),
                    pullbacks=sum(
                        s["family"] == "trend_pullback"
                        for stock in stocks
                        for s in stock.signals
                    ),
                )
                for name, stocks in variants.items()
            },
        )
    calendar = pd.DatetimeIndex(
        [pd.Timestamp(p["date"]) + pd.Timedelta(hours=15) for p in baseline["equity"]]
    )
    suite = str(uuid4())
    plan = configuration(baseline)
    summary = {}
    for name in CASES:
        summary[name] = {}
        for window, (start, end) in WINDOWS.items():
            for stressed in (False, True):
                cfg = replace(
                    plan,
                    base=replace(plan.base, slippage=0.001 if stressed else 0.0005),
                )
                clock = calendar[
                    (calendar >= start)
                    & (calendar < pd.Timestamp(end) + pd.Timedelta(days=1))
                ]
                data = window_series(variants[name], start, end)
                r = simulate(
                    data,
                    clock,
                    "swing_60f_v8" if name == "baseline" else "wyckoff_60f_v31",
                    cfg,
                    market_policy=MarketPolicy("none") if name == "baseline" else None,
                    wyckoff_policy=None if name == "baseline" else PROTECTION,
                )
                r["metrics"]["worst_trade_return_pct"] = min(
                    (t["return_pct"] for t in r["trades"]), default=None
                )
                accounted = (
                    cfg.base.initial_cash
                    + sum(t["pnl"] for t in r["trades"])
                    + sum(
                        p["unrealized_pnl"] + p["partial_realized_pnl"]
                        for p in r["open_positions"]
                    )
                )
                if abs(accounted - r["metrics"]["ending_equity"]) > 1e-5:
                    raise AssertionError("量价资金账不平")
                r = with_benchmarks(r, baseline["equity"])
                if name == "baseline" and window == "full":
                    prior = prior_stress if stressed else baseline
                    for key in (
                        "metrics",
                        "equity",
                        "open_positions",
                        "config",
                        "execution_audit",
                    ):
                        if json.loads(json.dumps(r[key])) != prior[key]:
                            raise AssertionError(f"Old baseline changed: {key}")
                    if [{**t, "entry_evidence": None} for t in r["trades"]] != prior[
                        "trades"
                    ]:
                        raise AssertionError("Old trades changed")
                r.update(
                    run_id=str(uuid4()),
                    suite_id=suite,
                    strategy_id="swing_60f_v8"
                    if name == "baseline"
                    else "wyckoff_60f_v31",
                    strategy_version=VERSION,
                    strategy_config=baseline["strategy_config"]
                    if name == "baseline"
                    else dict(
                        entry_method="frozen_v21"
                        if name == "volume_management"
                        else "price_volume",
                        wyckoff=asdict(CONFIG),
                    ),
                    timeframe="1D",
                    universe_mode="fixed_snapshot",
                    start=start,
                    end=end,
                    diagnostics=dict(
                        research_kind="wyckoff",
                        research_variant=name + ("__cost_stress" if stressed else ""),
                        in_sample=True,
                        independent_validation=False,
                        eligible_for_default=False,
                        research_selected=False,
                        fixed_primary=name == "price_volume_primary",
                        research_status="experimental",
                        warnings=declared["warnings"],
                        universe_size=len(original),
                        usable_stocks=len(data),
                        trend_timeframe="60F",
                        entry_timeframe="1D",
                        execution_schedule="after_close",
                        minimum_quality_score=72,
                        quality_scale="wyckoff-native-0-100"
                        if name == "price_volume_primary"
                        else "v8-native-0-120",
                    ),
                )
                label = window + ("__stress" if stressed else "")
                write_result(folder / name / label, r)
                summary[name][label] = r["metrics"]
                logger.info(
                    "%s %s return=%.4f%% worst=%s",
                    name,
                    label,
                    r["metrics"]["total_return_pct"],
                    r["metrics"]["worst_trade_return_pct"],
                )
    m = summary["price_volume_primary"]
    normal = m["full"]
    stress = m["full__stress"]
    passed = normal["total_return_pct"] >= 8 and all(
        v["closed_trades"] >= 30
        and v["max_drawdown_pct"] <= 8
        and v["worst_trade_return_pct"] is not None
        and v["worst_trade_return_pct"] >= -8
        and v["total_return_pct"] > 0
        for v in (normal, stress)
    )
    value = dict(
        primary="price_volume_primary",
        historical_target_met=passed,
        independent_validation=False,
        eligible_for_default=False,
        results=summary,
    )
    write_json(folder / "comparison.json", value)
    print(
        {
            name: dict(
                full=v["full"]["total_return_pct"],
                stress=v["full__stress"]["total_return_pct"],
            )
            for name, v in summary.items()
        }
    )


if __name__ == "__main__":
    main()
