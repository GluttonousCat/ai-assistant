"""V25: fixed dual-index gates on the frozen monthly-ranked 60F candidates."""

import argparse
from pathlib import Path

from core.config import get_config
from workflows.quant.studies.provenance import digest, freeze, source_hashes
from workflows.quant.backtests.mtf import WINDOWS
from workflows.quant.studies.quality_60f import (
    BASELINE,
    STRESS,
    ORIGINAL,
    RANKED,
    MINIMUM_QUALITY,
    read_report,
    run,
)
from skills.quant.market_context import MODES

VERSION = "quality72-online-index-gates-v25"
FOLDER = "quality72_market_v25"
CASES = {"baseline": {}, **{mode: {"market_mode": mode} for mode in MODES}}


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
        "workflows/quant/studies/online_market_gate.py",
        "workflows/quant/studies/quality_60f.py",
        "skills/quant/market_context.py",
        "skills/quant/candidate_quality.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/stop_orders.py",
        "skills/quant/tranches.py",
        "skills/quant/models.py",
        "skills/quant/metrics.py",
        "workflows/quant/backtests/mtf.py",
        "workflows/quant/studies/mainboard_60f.py",
        "skills/quant/report.py",
    ]
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=VERSION,
            cases=CASES,
            windows=WINDOWS,
            minimum_quality=MINIMUM_QUALITY,
            input_sha256={
                str(Path(p).relative_to(root) if Path(p).is_absolute() else p): digest(
                    root / p
                )
                for p in [ORIGINAL, RANKED, baseline_path, stress_path]
            },
            code_sha256=source_hashes(root, sources),
            selection="Six fixed cases; Jan-Jun normal AND stressed positive/PF>1/>=30 campaigns/MDD<=8%/worst>=-8%; maximize minimum return/max(MDD,1). Freeze before later windows.",
            target="Full funded net return>=8%; both costs MDD<=8%, worst>=-8%, >=30 campaigns and stressed return>0.",
            warnings=[
                "2026 has been repeatedly inspected; later segments are NOT independent validation. Fixed current2333 non-ST mainboard union has membership/survivorship bias.",
                "Monthly prediction/quality72 gate remain fixed. V8 native quality0-120 is separate from priority and frontend V7 score; warmup cash included.",
                "Index evidence was frozen causally from completed native60F bars. Daily trend mode uses index session closes, not an individual daily candle-shape gate.",
                "Only entry/add market gates change. Original60F trigger,532 buys/sales,2.5% planned fill distance,5% protection,.35% account risk,10% stock cap and next-open execution stay unchanged.",
                "No preplaced intraday stops, leverage, future membership, live orders or automatic default changes. Gap/limit losses may exceed planned risk.",
            ],
        ),
        source_root=root,
    )
    if args.declare_only:
        print("Declared5 fixed index gates and after-close control.")
        return
    run(root, folder, declared, baseline, stress, cases=CASES, version=VERSION)


if __name__ == "__main__":
    main()
