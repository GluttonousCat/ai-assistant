"""V27: fixed confirmed-support protection tests on frozen monthly priority."""

import argparse
from dataclasses import replace
from pathlib import Path
import pickle

import numpy as np
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
    MINIMUM_QUALITY,
    digest,
    freeze,
    read_report,
    run,
)
from skills.quant.support_protection import attach_support

logger = get_logger(__name__)
VERSION = "quality72-confirmed-support-v27"
FOLDER = "quality72_support_v27"
CASES = {
    "baseline": {},
    **{
        f"support{after:g}r_buffer{buffer:g}_{weights}": dict(
            support_protection=dict(after_r=after, buffer_atr=buffer),
            entry_weights=(0.5, 0.3, 0.2) if weights == "532" else (0.4, 0.4, 0.2),
            exit_weights=(0.5, 0.3, 0.2) if weights == "532" else (0.4, 0.4, 0.2),
        )
        for after in (0.5, 1.0)
        for buffer in (0.25, 0.5)
        for weights in ("532", "442")
    },
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
    with (root / RANKED).open("rb") as stream:
        codes = [s.code for s in pickle.load(stream)["series"] if s.signals]
    minute_files = {
        code: root / "output/quant/mainboard_60f_v8/minutes" / f"{code}.pkl"
        for code in codes
    }
    sources = [
        "jobs/research_support_tranches_60f.py",
        "jobs/research_quality_60f.py",
        "skills/quant/support_protection.py",
        "skills/quant/candidate_quality.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranches.py",
        "skills/quant/models.py",
        "skills/quant/prepare.py",
        "skills/quant/market_context.py",
        "skills/quant/metrics.py",
        "skills/quant/report.py",
        "jobs/backtest_mtf.py",
        "jobs/research_mainboard_60f.py",
    ]
    inputs = [ORIGINAL, RANKED, baseline_path, stress_path, *minute_files.values()]
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
                for p in inputs
            },
            code_sha256={p: digest(root / p) for p in sources},
            selection="Jan-Jun normal AND doubled slippage positive/PF>1/>=30 campaigns/MDD<=8%/worst>=-8%; maximize minimum return/max(MDD,1). Freeze before later known windows.",
            target="Full funded net>=8%; both costs MDD<=8%,worst>=-8%,>=30 campaigns, stressed net>0.",
            warnings=[
                "All2026 repeatedly inspected; later windows are NOT independent out-of-sample validation. Fixed current2333 mainboard non-ST union has survivorship/membership bias.",
                "Frozen monthly causal model, original60F signals, native quality72 gate, warmup cash,2.5% actual fill distance,.35% account risk,10% stock cap unchanged.",
                "Higher low: strict native60F pivot with two left and two right bars; second confirmed pivot exceeds prior pivot. Confirmation timestamp prevents future pivots from appearing early.",
                "Only supports formed after actual entry may protect. After .5R or1R close progress and existing60F add confirmation, raise stop to support minus .25 or .5ATR, never lower it.",
                "Support changes only after close; additions/exits execute next legal open. No intraday conditional orders or added individual daily-candle gate.",
                "442/532 buy-and-sell plans compared with unchanged5%/8% profit thresholds and20-session hold. No averaging down, increased risk budget or guaranteed gap loss limit.",
                "Source60F and daily OHLC may differ slightly; minute prices are not rewritten. Corporate-action factor shares remain an approximation.",
                "No automatic default changes, broker orders or a claim of independently validated returns.",
            ],
        ),
    )
    for rel in sources:
        target = folder / "source" / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / rel).read_bytes())
    if args.declare_only:
        print("Declared8 support-protection cases and frozen after-close control.")
        return

    def transform(stocks):
        result, supports = [], 0
        for n, stock in enumerate(stocks, 1):
            if stock.signals:
                stock = attach_support(pd.read_pickle(minute_files[stock.code]), stock)
                supports += int(np.isfinite(stock.data["confirmed_higher_low"]).sum())
            else:
                stock = replace(
                    stock,
                    data={
                        **stock.data,
                        "confirmed_higher_low": np.full(len(stock.times), np.nan),
                        "higher_low_origin": np.full(
                            len(stock.times), pd.NaT.value, dtype=np.int64
                        ),
                        "higher_low_known": np.full(
                            len(stock.times), pd.NaT.value, dtype=np.int64
                        ),
                    },
                )
            result.append(stock)
            if n % 500 == 0 or n == len(stocks):
                logger.info("Confirmed support arrays %s/%s", n, len(stocks))
        write_json(
            folder / "support_audit.json",
            dict(
                stocks_with_signals=len(codes),
                daily_support_observations=supports,
                right_confirmation_bars=2,
                decision="after_close",
            ),
        )
        return result

    run(
        root,
        folder,
        declared,
        baseline,
        stress,
        cases=CASES,
        version=VERSION,
        transform=transform,
    )


if __name__ == "__main__":
    main()
