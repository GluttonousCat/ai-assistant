"""V29: fixed auxiliary-label alignment study; no default strategy changes."""

import argparse
from dataclasses import asdict, replace
from pathlib import Path
import pickle

from core.config import get_config
from core.logger import get_logger
from jobs.backtest_mtf import WINDOWS
from jobs.research_mainboard_60f import write_json
from jobs.research_market_context import load_indices, REFERENCE
from jobs.research_quality_60f import (
    BASELINE,
    STRESS,
    ORIGINAL,
    RANKED,
    digest,
    freeze,
    read_report,
    run,
)
from skills.quant.label_research import learn_priorities, restore_candidate_features
from skills.quant.online_priority import OnlineConfig

logger = get_logger(__name__)
VERSION = "quality72-auxiliary-label-alignment-v29"
FOLDER = "quality72_labels_v29"
BASE = OnlineConfig()
CONFIGS = {
    "baseline": BASE,
    "risk2.5_hold10_profit5": replace(BASE, label_max_risk_pct=2.5),
    "risk2.5_hold20_profit5": replace(BASE, label_max_risk_pct=2.5, label_horizon=20),
    "risk2.5_hold10_profit8": replace(BASE, label_max_risk_pct=2.5, label_profit_pct=8),
    "risk2.5_hold20_profit8": replace(
        BASE, label_max_risk_pct=2.5, label_horizon=20, label_profit_pct=8
    ),
    "risk4_hold20_profit5": replace(BASE, label_horizon=20),
    "risk4_hold10_profit8": replace(BASE, label_profit_pct=8),
    "risk2.5_warmup250": replace(BASE, label_max_risk_pct=2.5, min_samples=250),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    folder = root / "output/quant" / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    baseline_path, baseline = read_report(root / BASELINE)
    stress_path, stress = read_report(root / STRESS)
    frames, calendar, index_audit = load_indices(root / "output/quant/market_v11", root)
    sources = [
        "jobs/research_label_alignment_60f.py",
        "jobs/research_quality_60f.py",
        "skills/quant/label_research.py",
        "skills/quant/online_priority.py",
        "skills/quant/candidate_quality.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranches.py",
        "skills/quant/models.py",
        "skills/quant/metrics.py",
        "skills/quant/market_context.py",
        "skills/quant/stop_orders.py",
        "skills/quant/support_protection.py",
        "skills/quant/report.py",
        "jobs/backtest_mtf.py",
        "jobs/research_mainboard_60f.py",
        "jobs/research_market_context.py",
    ]
    inputs = [ORIGINAL, RANKED, baseline_path, stress_path, REFERENCE]
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=VERSION,
            model_configs={k: asdict(v) for k, v in CONFIGS.items()},
            windows=WINDOWS,
            input_sha256={
                str(Path(p).relative_to(root) if Path(p).is_absolute() else p): digest(
                    root / p
                )
                for p in inputs
            },
            code_sha256={p: digest(root / p) for p in sources},
            indices=index_audit,
            selection="Seven new fixed models and baseline. Select on Jan-Jun normal AND double-slippage positive/PF>1/>=30 campaigns/MDD<=8%/worst>=-8%; maximize minimum return/max(MDD,1), freeze before known later windows.",
            target="Full funded net>=8%; both costs MDD<=8%,worst>=-8%,>=30 campaigns, stressed return>0.",
            warnings=[
                "All2026 previously inspected; NOT independent out-of-sample validation. Fixed current2333 mainboard non-ST union retains survivorship/membership bias.",
                "Baseline auxiliary labels allow4% entry distance/10-session holding/5% close target; actual portfolio allows2.5% and20 sessions with532 execution. New cases align these label assumptions or shorten cash warmup to250 known labels.",
                "Auxiliary labels remain single-entry market-relative outcomes, not fully replicated staged portfolio outcomes. No claim of exact execution-label alignment.",
                "19 frozen completed60F features, monthly ridge100 and all portfolio execution/risk settings remain fixed. Predictions are available even when current labels remain unresolved.",
                "All original9789 candidates enter feature/training construction, not just the615 old selected candidates. Only two causal volatility arrays are restored from the old cache by code/time/raw-price key, never its forecasts or future membership.",
                "Each monthly fit/scaler sees only labels completed before that month. Full Jan1-Nov12 includes all warmup cash;250/500 overlapping outcomes are not independent samples.",
                "Native quality>=72 uses v8's0-120 scale, separate from prediction and frontend V7 score. Next legal open, fees, limits, T+1,2.5% actual fill cap,.35% account risk,10% stock cap and532 remain fixed.",
                "All cost stresses replay the whole portfolio using the same previously fitted model. Planned risk does not guarantee gap losses; no leverage, orders, external fetching or automatic defaults.",
            ],
        ),
    )
    for rel in sources:
        dest = folder / "source" / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((root / rel).read_bytes())
    if args.declare_only:
        print("Declared7 label/warmup alternatives and frozen baseline.")
        return
    cache = folder / "learned.pkl"
    if args.resume:
        with cache.open("rb") as stream:
            variants = pickle.load(stream)
    else:
        with (root / ORIGINAL).open("rb") as stream:
            original = pickle.load(stream)["series"]
        with (root / RANKED).open("rb") as stream:
            cached = pickle.load(stream)["series"]
        stocks = restore_candidate_features(original, cached)
        variants = {}
        for name, cfg in CONFIGS.items():
            learned = learn_priorities(stocks, calendar, frames, cfg)
            write_json(folder / f"{name}_models.json", learned["models"])
            variants[name] = learned
            logger.info("Trained %s: %s", name, learned["audit"])
        frozen = {s.code: s for s in cached}
        for stock in variants["baseline"]["series"]:
            expected = {(s["i"], s["leg"]): s for s in frozen[stock.code].signals}
            actual = {(s["i"], s["leg"]): s for s in stock.signals}
            if set(expected) != set(actual) or any(
                abs(expected[k]["score"] - actual[k]["score"]) > 1e-10 for k in expected
            ):
                raise AssertionError(f"Baseline model parity failed: {stock.code}")
        write_json(
            folder / "model_audit.json",
            {name: result["audit"] for name, result in variants.items()},
        )
        with cache.open("wb") as stream:
            pickle.dump(variants, stream, pickle.HIGHEST_PROTOCOL)
    run(
        root,
        folder,
        declared,
        baseline,
        stress,
        cases={name: {} for name in CONFIGS},
        version=VERSION,
        model_variants=variants,
    )


if __name__ == "__main__":
    main()
