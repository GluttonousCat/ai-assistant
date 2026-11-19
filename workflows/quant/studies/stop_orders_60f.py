"""V26 fixed protective-stop scenarios; never install a live broker order."""

import argparse
from dataclasses import asdict
from pathlib import Path
import pickle

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from workflows.quant.studies.provenance import digest, freeze, source_hashes
from workflows.quant.backtests.mtf import WINDOWS
from workflows.quant.studies.mainboard_60f import write_json
from workflows.quant.studies.quality_60f import (
    BASELINE,
    STRESS,
    ORIGINAL,
    RANKED,
    MINIMUM_QUALITY,
    configuration,
    read_report,
    run,
)
from skills.quant.stop_orders import prepare_stop_bars

logger = get_logger(__name__)
VERSION = "quality72-preplaced-stops-v26"
FOLDER = "quality72_stops_v26"
CASES = {
    "baseline": {},
    **{
        f"stop_protect{protection:g}_sell{weights}": dict(
            preplaced_stops=True,
            protect_at_pct=protection,
            exit_weights=(0.5, 0.3, 0.2) if weights == "532" else (0.4, 0.4, 0.2),
        )
        for protection in (2.5, 3.0, 5.0)
        for weights in ("532", "442")
    },
}


def execute_study(
    root,
    folder,
    version,
    cases,
    declare_only=False,
    extra_sources=(),
    extra_warnings=(),
):
    folder.mkdir(parents=True, exist_ok=True)
    baseline_path, baseline = read_report(root / BASELINE)
    stress_path, stress = read_report(root / STRESS)
    with (root / RANKED).open("rb") as stream:
        stocks = [s for s in pickle.load(stream)["series"] if s.signals]
    minute_files = {
        s.code: root / "output/quant/mainboard_60f_v8/minutes" / f"{s.code}.pkl"
        for s in stocks
    }
    files = [ORIGINAL, RANKED, baseline_path, stress_path, *minute_files.values()]
    sources = [
        "workflows/quant/studies/stop_orders_60f.py",
        "workflows/quant/studies/quality_60f.py",
        "skills/quant/candidate_quality.py",
        "skills/quant/stop_orders.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranches.py",
        "skills/quant/models.py",
        "skills/quant/market_context.py",
        "skills/quant/report.py",
        "skills/quant/metrics.py",
        "workflows/quant/backtests/mtf.py",
        "workflows/quant/studies/mainboard_60f.py",
        "skills/quant/support_protection.py",
        *extra_sources,
    ]
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=version,
            cases=cases,
            windows=WINDOWS,
            minimum_native_quality=MINIMUM_QUALITY,
            input_sha256={
                str(Path(p).relative_to(root) if Path(p).is_absolute() else p): digest(
                    root / p
                )
                for p in files
            },
            code_sha256=source_hashes(root, sources),
            base_plan=asdict(configuration(baseline)),
            selection="Jan-Jun only, both costs: positive/PF>1/>=30 campaigns/MDD<=8%/worst>=-8%; maximize minimum return/max(MDD,1). Freeze before later known windows.",
            target="Full funded net return>=8%; both costs MDD<=8%, worst>=-8%, >=30 campaigns; stressed return>0.",
            warnings=[
                "Repeated 2026 research; later windows have already been inspected, NOT independent validation or a live recommendation.",
                "Fixed current non-ST2333 mainboard union, with membership/survivorship bias; warmup cash included.",
                "Quality72 uses frozen v8 native0-120 score, separate from model priority and frontend V7 score.",
                "Individual triggers remain60F; daily execution prices and monthly model are unchanged. 532 entry,2.5% planned entry distance,.35% account risk,10% stock cap.",
                "OPT-IN PREPLACED STOP SCENARIO. A working user would need broker-side conditional orders; this project does not submit orders. After-close default remains unchanged.",
                "Baseline stops use prior-session protection; intraday updates require explicit policy and only affect subsequent bars. Verified60F bars must match daily raw OHLC within one cent. Mismatched/missing dates use labeled1D OHLC fallback. Intrabar touch is an approximation, not a tick/order-book fill guarantee.",
                "Gaps fill at native open, not the stop; price limits, per-bar1% participation, 100-share lots and per-lot T+1 constrain sales. First-day breaches can only sell settled lots.",
                "Intraday stop proceeds are unavailable for that session's opening purchases. Unfilled activated stops stay queued, even if price subsequently rebounds.",
                "No daily-high-based same-day stop tightening. Slippage doubles by replaying the complete portfolio path, not by subtracting fees from old trades.",
                "No leverage or increased account/position risk. Factor-share approximation, no cash corporate-action ledger. No hard bound on gaps or realized losses.",
                *extra_warnings,
            ],
        ),
        source_root=root,
    )
    if declare_only:
        print(f"Declared {len(cases)} fixed execution cases.")
        return
    stop_bars, fallback = {}, {}
    for n, stock in enumerate(stocks, 1):
        stop_bars[stock.code] = prepare_stop_bars(
            stock, pd.read_pickle(minute_files[stock.code]), allow_daily_fallback=True
        )
        bad_dates = [
            str(day)
            for day, bars in stop_bars[stock.code].items()
            if bars[0].resolution == "1D_fallback"
        ]
        if bad_dates:
            fallback[stock.code] = bad_dates
        if n % 100 == 0 or n == len(stocks):
            logger.info("Validated native stop bars %s/%s", n, len(stocks))
    write_json(
        folder / "stop_data_audit.json",
        dict(
            stocks=len(stocks),
            fallback_dates=fallback,
            fallback_stock_count=len(fallback),
            fallback_date_count=sum(map(len, fallback.values())),
            minute_prices_rewritten=False,
            default_strict_validation=True,
        ),
    )
    run(
        root,
        folder,
        declared,
        baseline,
        stress,
        cases=cases,
        version=version,
        stop_bars=stop_bars,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    execute_study(
        root, root / "output/quant" / FOLDER, VERSION, CASES, args.declare_only
    )


if __name__ == "__main__":
    main()
