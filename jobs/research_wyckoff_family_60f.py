"""Compare a family-specific hybrid with its unchanged and trend-only controls."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import pickle
from uuid import uuid4

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from jobs.backtest_mtf import WINDOWS, window_series
from jobs.research_mainboard_60f import write_json
from jobs.research_quality_60f import (
    BASELINE,
    STRESS,
    configuration,
    digest,
    freeze,
    read_report,
)
from skills.quant.market_context import MarketPolicy
from skills.quant.report import with_benchmarks, write_result
from skills.quant.support_protection import SupportProtection
from skills.quant.tranche_engine import simulate
from skills.quant.tranche_prepare import attach_entry_session_low
from skills.quant.wyckoff_hybrid import combine_families

VERSION = "wyckoff-family-management-v38"
logger = get_logger(__name__)


def main():
    root = Path(get_config().root_dir)
    source = root / "output/quant/wyckoff_price_volume_v33/prepared.pkl"
    manifest = source.with_name("declaration.json")
    folder = root / "output/quant/wyckoff_family_v38"
    folder.mkdir(parents=True, exist_ok=True)
    baseline_path, baseline = read_report(root / BASELINE)
    stress_path, stress = read_report(root / STRESS)
    prior = json.loads(manifest.read_text(encoding="utf-8"))
    if prior["strategy_version"] != "wyckoff-price-volume-60f-v33":
        raise ValueError("量价候选来源版本不匹配")
    protection = SupportProtection(**prior["protection"])
    with source.open("rb") as stream:
        prepared = pickle.load(stream)
    ranked = prepared["baseline"]
    minute_paths = {
        stock.code: root / "output/quant/mainboard_60f_v8/minutes" / f"{stock.code}.pkl"
        for stock in ranked
        if any(s["family"] == "trend_pullback" for s in stock.signals)
    }
    input_hashes = {str(p.relative_to(root)): digest(p) for p in minute_paths.values()}
    if any(
        prior["input_sha256"].get(path) != value for path, value in input_hashes.items()
    ):
        raise ValueError("首日风险复核的分钟行情与冻结准备输入不一致")
    code = [
        "jobs/research_wyckoff_family_60f.py",
        "jobs/backtest_mtf.py",
        "jobs/research_quality_60f.py",
        "skills/quant/wyckoff_hybrid.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranche_prepare.py",
        "skills/quant/prepare.py",
        "skills/quant/registry.py",
        "skills/quant/metrics.py",
        "skills/quant/models.py",
        "skills/quant/report.py",
        "skills/quant/tranches.py",
        "skills/quant/support_protection.py",
        "skills/quant/market_context.py",
    ]
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=VERSION,
            windows=WINDOWS,
            minimum_quality_score=72,
            cases=[
                "baseline",
                "pullback_only",
                "family_hybrid",
                "family_hybrid_two_slots",
                "family_hybrid_first_session_review",
                "family_hybrid_first_session_reduce",
            ],
            reversal_slot_limit=2,
            volume_protection=asdict(protection),
            inputs={
                **input_hashes,
                **{
                    str(p.relative_to(root)): digest(p)
                    for p in (source, manifest, baseline_path, stress_path)
                },
            },
            code_sha256={name: digest(root / name) for name in code},
            warnings=[
                "Fixed 2026 mainboard universe, previously inspected history with membership/survivorship bias.",
                "Trend pullbacks retain existing MA, volume and model priorities with existing position management. The hybrid replaces only the reversal candidates and their position management with v33 price-volume rules.",
                "A common cash account gives trend entries first access to same-day capacity. The reversal queue uses native quality points. Forecast and quality scores have separate units.",
                "One fixed allocation variant limits concurrent reversals to two of ten positions. Cases are all disclosed and no full-period winner is selected.",
                "On a trend pullback's first holding day, a completed 60F session that traded through the planned stop triggers a next-open risk exit. Missing sessions are audited. Reversal trade management is unchanged.",
                "The fixed reduce variant sells the first scheduled tranche, disables additional entries and advances the same cumulative exit schedule. Hard stops still take precedence.",
                "Both families use existing index safeguards, completed 60F after-close decisions, next legal open, 532 and original cash, fees, limits, settlement, and risk caps.",
            ],
        ),
    )
    for name in code:
        target = folder / "source" / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / name).read_bytes())
    hybrid = combine_families(ranked, prepared["price_volume_primary"])
    reviewed = [
        attach_entry_session_low(pd.read_pickle(minute_paths[stock.code]), stock)
        if stock.code in minute_paths
        else stock
        for stock in hybrid
    ]
    pullbacks = [
        replace(s, signals=[x for x in s.signals if x["family"] == "trend_pullback"])
        for s in ranked
    ]
    cases = dict(
        baseline=ranked,
        pullback_only=pullbacks,
        family_hybrid=hybrid,
        family_hybrid_two_slots=hybrid,
        family_hybrid_first_session_review=reviewed,
        family_hybrid_first_session_reduce=reviewed,
    )
    calendar = pd.DatetimeIndex(
        [pd.Timestamp(p["date"]) + pd.Timedelta(hours=15) for p in baseline["equity"]]
    )
    plan, suite = configuration(baseline), str(uuid4())
    comparison = {}
    for name, stocks in cases.items():
        output = comparison[name] = {}
        mixed = name.startswith("family_hybrid")
        review = {
            "family_hybrid_first_session_review": "exit",
            "family_hybrid_first_session_reduce": "reduce",
        }.get(name)
        strategy = "wyckoff_60f_v31" if mixed else "swing_60f_v8"
        for window, (start, end) in WINDOWS.items():
            clock = calendar[
                (calendar >= start)
                & (calendar < pd.Timestamp(end) + pd.Timedelta(days=1))
            ]
            series = window_series(stocks, start, end)
            for stressed in (False, True):
                cfg = replace(
                    plan,
                    base=replace(plan.base, slippage=0.001 if stressed else 0.0005),
                )
                result = simulate(
                    series,
                    clock,
                    strategy,
                    cfg,
                    market_policy=MarketPolicy("none"),
                    wyckoff_policy=protection if mixed else None,
                    wyckoff_families=("base_rebound",) if mixed else None,
                    wyckoff_slot_limit=declared["reversal_slot_limit"]
                    if review or name == "family_hybrid_two_slots"
                    else None,
                    first_session_stop_review=review,
                )
                result = with_benchmarks(result, baseline["equity"])
                metrics = result["metrics"]
                metrics["worst_trade_return_pct"] = min(
                    (t["return_pct"] for t in result["trades"]), default=None
                )
                pnl = sum(t["pnl"] for t in result["trades"]) + sum(
                    p["unrealized_pnl"] + p["partial_realized_pnl"]
                    for p in result["open_positions"]
                )
                if abs(cfg.base.initial_cash + pnl - metrics["ending_equity"]) > 1e-5:
                    raise AssertionError("组合期末资金对账失败")
                if name == "baseline" and window == "full":
                    expected = stress if stressed else baseline
                    for key in (
                        "metrics",
                        "equity",
                        "open_positions",
                        "config",
                        "execution_audit",
                    ):
                        if json.loads(json.dumps(result[key])) != expected[key]:
                            raise AssertionError(f"对照撮合发生变化: {key}")
                    if [
                        {**t, "entry_evidence": None} for t in result["trades"]
                    ] != expected["trades"]:
                        raise AssertionError("对照成交发生变化")
                label = window + ("__stress" if stressed else "")
                result.update(
                    run_id=str(uuid4()),
                    suite_id=suite,
                    strategy_id=strategy,
                    strategy_version=VERSION,
                    strategy_config={
                        "case": name,
                        "original": baseline["strategy_config"],
                        "volume": prior["config"] if mixed else None,
                    },
                    timeframe="1D",
                    universe_mode="fixed_snapshot",
                    start=start,
                    end=end,
                    diagnostics=dict(
                        research_kind="wyckoff",
                        research_variant=name + ":" + label,
                        in_sample=True,
                        independent_validation=False,
                        eligible_for_default=False,
                        warnings=declared["warnings"],
                        minimum_quality_score=72,
                        universe_size=len(stocks),
                        trend_timeframe="60F",
                        entry_timeframe="1D",
                        execution_schedule="after_close",
                    ),
                )
                write_result(folder / name / label, result)
                output[label] = metrics
                logger.info(
                    "%s %s: net %.4f%%, worst %s",
                    name,
                    label,
                    metrics["total_return_pct"],
                    metrics["worst_trade_return_pct"],
                )
    write_json(folder / "comparison.json", comparison)


if __name__ == "__main__":
    main()
