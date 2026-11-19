"""V28: preplaced breakeven protection updated only AFTER completed60F bars."""

import argparse
from pathlib import Path

from core.config import get_config
from workflows.quant.studies.stop_orders_60f import execute_study

VERSION = "quality72-completed60f-protection-v28"
FOLDER = "quality72_intraday_protection_v28"
CASES = {
    "baseline": {},
    "fixed_stop": dict(preplaced_stops=True),
    **{
        f"protect{after:g}r_min{minimum:g}": dict(
            preplaced_stops=True,
            intraday_protection=dict(after_r=after, minimum_profit_pct=minimum),
        )
        for after in (0.5, 0.75, 1.0)
        for minimum in (1.0, 1.5)
    },
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    execute_study(
        root,
        root / "output/quant" / FOLDER,
        VERSION,
        CASES,
        args.declare_only,
        extra_sources=("workflows/quant/studies/intraday_protection_60f.py",),
        extra_warnings=(
            "Explicit automated60F protective-order scenario, not manual after-work execution. No actual broker integration or orders.",
            "At a completed native60F close, after .5/.75/1R AND1/1.5% profit, raise stop to fee/slippage-adjusted cost floor. It can first trigger in the NEXT bar; today's high cannot retroactively cause a fill.",
            "A first-day trigger still cannot sell T+1-locked lots; the activated order carries to the next tradable bar/session. It is not canceled on a rebound.",
            "Day-level fallback can only update after15:00. Original60F signal definitions,532 acquisition/exit plan and5/8% profit targets stay fixed.",
        ),
    )


if __name__ == "__main__":
    main()
