"""V30: confirm relative strength with a causal absolute-return forecast."""

import argparse
from dataclasses import asdict
from pathlib import Path
import pickle

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from jobs.backtest_mtf import WINDOWS
from jobs.research_mainboard_60f import write_json
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
from skills.quant.candidate_quality import attach_signal_quality
from skills.quant.label_research import (
    fit_absolute_head,
    confirm_absolute_forecasts,
    restore_candidate_features,
)
from skills.quant.online_priority import OnlineConfig

logger = get_logger(__name__)
VERSION = "quality72-dual-return-confirmation-v30"
FOLDER = "quality72_dual_forecast_v30"
CONFIRMATIONS = {
    "absolute0": (0.0, 0.0),
    "absolute0.25": (0.25, 0.0),
    "absolute0.5": (0.5, 0.0),
    "blend_equal_absolute0": (0.0, 0.5),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    folder = root / "output/quant" / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    baseline_path, baseline = read_report(root / BASELINE)
    stress_path, stress = read_report(root / STRESS)
    sources = [
        "jobs/research_dual_forecast_60f.py",
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
    ]
    inputs = [ORIGINAL, RANKED, baseline_path, stress_path]
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=VERSION,
            confirmations=CONFIRMATIONS,
            windows=WINDOWS,
            model=asdict(OnlineConfig()),
            input_sha256={
                str(Path(p).relative_to(root) if Path(p).is_absolute() else p): digest(
                    root / p
                )
                for p in inputs
            },
            code_sha256={p: digest(root / p) for p in sources},
            selection="Four new dual-head confirmations and baseline. Jan-Jun both costs: positive/PF>1/>=30 campaigns/MDD<=8%/worst>=-8%; maximize minimum return/max(MDD,1); freeze before later known windows.",
            target="Full funded net>=8%, both costs MDD<=8%,worst>=-8%,>=30 campaigns, stressed net>0.",
            warnings=[
                "2026 repeatedly inspected, NOT independent out-of-sample validation. Fixed current2333 mainboard non-ST pool has membership/survivorship bias.",
                "A stock can outperform a falling index and still lose money. New confirmation requires a nonnegative/0.25/0.5% absolute-return forecast; a fixed equal-weight ranking case is also compared.",
                "Historical absolute models in v15-v17 failed; this is a declared joint-confirmation test on the existing relative model with current2.5% execution, not evidence those old models worked.",
                "Relative forecasts remain frozen v21. Absolute ridge100 uses all original9789 candidates, 19 causal features, min500 completed prior labels,10-session/4%/5% auxiliary labels; no current outcome controls forecast eligibility.",
                "Both monthly heads/scalers train only labels completed before the month. Auxiliary predictions are not promised returns or exact532 outcomes.",
                "Quality>=72 uses original v8 native0-120 grade, not either forecast or frontend V7 score. Both heads must be available; cash warmup stays in full-period returns.",
                "Same after-close signals/next-open execution,532,2.5% actual fill cap,.35% account risk,10% stock cap, fees, limits and T+1. No intraday stop scenario or risk increases.",
                "Cost stress replays the complete portfolio without refitting on stressed outcomes. No automatic default change, leverage, real orders or external fetching; gap losses are not bounded by planned risk.",
            ],
        ),
    )
    for rel in sources:
        dest = folder / "source" / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((root / rel).read_bytes())
    if args.declare_only:
        print("Declared4 absolute-return confirmation variants and relative baseline.")
        return
    with (root / ORIGINAL).open("rb") as stream:
        original = pickle.load(stream)["series"]
    with (root / RANKED).open("rb") as stream:
        cached = pickle.load(stream)
    stocks = restore_candidate_features(original, cached["series"])
    relative = dict(
        series=attach_signal_quality(cached["series"], original),
        models=cached["models"],
        model_config=asdict(OnlineConfig()),
    )
    calendar = pd.DatetimeIndex(
        [pd.Timestamp(p["date"]) + pd.Timedelta(hours=15) for p in baseline["equity"]]
    )
    absolute = fit_absolute_head(stocks, calendar, OnlineConfig())
    write_json(folder / "absolute_models.json", absolute["models"])
    variants = {
        name: confirm_absolute_forecasts(relative, original, absolute, *settings)
        for name, settings in CONFIRMATIONS.items()
    }
    for name, value in variants.items():
        logger.info("Confirmed %s: %s", name, value["audit"])
    write_json(
        folder / "forecast_audit.json",
        dict(
            absolute_rows=absolute["candidate_rows"],
            absolute_forecasts=len(absolute["predictions"]),
            variants={name: value["audit"] for name, value in variants.items()},
        ),
    )
    run(
        root,
        folder,
        declared,
        baseline,
        stress,
        cases={name: {} for name in ["baseline", *CONFIRMATIONS]},
        version=VERSION,
        model_variants=variants,
    )


if __name__ == "__main__":
    main()
