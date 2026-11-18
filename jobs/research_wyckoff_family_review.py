"""Replay two fixed changes to the continued mixed portfolio against its control."""

from dataclasses import replace
import json
from pathlib import Path
import pickle
from uuid import uuid4

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from jobs.backtest_mtf import WINDOWS, window_series
from jobs.research_mainboard_60f import write_json
from jobs.research_quality_60f import configuration, digest, freeze, read_report
from skills.quant.market_context import MarketPolicy
from skills.quant.report import with_benchmarks, write_result
from skills.quant.support_protection import SupportProtection
from skills.quant.tranche_engine import simulate

logger = get_logger(__name__)
VERSION = "wyckoff-family-review-v42"


def main():
    root = Path(get_config().root_dir)
    source = root / "output/quant/wyckoff_forward_v40"
    folder = root / "output/quant/wyckoff_family_review_v42"
    folder.mkdir(parents=True, exist_ok=True)
    cache, manifest = source / "prepared.pkl", source / "declaration.json"
    prior = json.loads(manifest.read_text(encoding="utf-8"))
    if prior["version"] != "wyckoff-forward-20261118-v40":
        raise ValueError("结构离场试验需要v40冻结组合输入")
    normal_path, normal = read_report(source / "hybrid/full")
    stress_path, stress = read_report(source / "hybrid/full__stress")
    if normal["config"]["min_trend_holding_bars"] != 4:
        raise ValueError("原回调结构退出等待期不匹配")
    protection = SupportProtection(**normal["config"]["wyckoff_policy"])
    cases = {
        "control": {"trend_holding": 4, "reversal_slots": 2},
        "structure_from_third_session": {"trend_holding": 3, "reversal_slots": 2},
        "single_reversal_slot": {"trend_holding": 4, "reversal_slots": 1},
    }
    windows = {
        **WINDOWS,
        "full": (normal["start"], normal["end"]),
    }
    code = [
        "jobs/research_wyckoff_family_review.py",
        "jobs/backtest_mtf.py",
        "jobs/research_quality_60f.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranches.py",
        "skills/quant/models.py",
        "skills/quant/report.py",
        "skills/quant/market_context.py",
        "skills/quant/support_protection.py",
        "skills/quant/metrics.py",
        "skills/quant/registry.py",
    ]
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=VERSION,
            cases=cases,
            windows=windows,
            minimum_quality_score=72,
            forward_cutoff=prior["cutoff"],
            inputs={
                str(p.relative_to(root)): digest(p)
                for p in (cache, manifest, normal_path, stress_path)
            },
            code_sha256={name: digest(root / name) for name in code},
            warnings=[
                "Fixed comparison motivated by losses observed in 2026. Every window is previously inspected; the last four days are now diagnostic, not fresh validation for this change.",
                "Stock candidates, model, price stops, 532 trims, reversal rules, index safeguards, risk budget, fees and total position limit follow v40. Compare the minimum trend-structure holding period and maximum simultaneous reversal slots as isolated changes.",
                "A structural reaction remains a close decision plus next legal open. T+1, actual limits and fees apply.",
            ],
        ),
    )
    for name in code:
        path = folder / "source" / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((root / name).read_bytes())
    with cache.open("rb") as stream:
        stocks = pickle.load(stream)["hybrid"]
    calendar = pd.DatetimeIndex(
        [pd.Timestamp(x["date"]) + pd.Timedelta(hours=15) for x in normal["equity"]]
    )
    plan = configuration(normal)
    suite, summary = str(uuid4()), {}
    if normal["config"]["wyckoff_slot_limit"] != cases["control"]["reversal_slots"]:
        raise ValueError("原反转席位数不匹配")
    review = normal["config"]["first_session_stop_review"]
    for case, settings in cases.items():
        summary[case] = {}
        for window, (start, end) in windows.items():
            clock = calendar[
                (calendar >= start)
                & (calendar < pd.Timestamp(end) + pd.Timedelta(days=1))
            ]
            local = window_series(stocks, start, end)
            for is_stress, source_report in ((False, normal), (True, stress)):
                cfg = replace(
                    plan,
                    base=replace(
                        plan.base,
                        min_trend_holding_bars=settings["trend_holding"],
                        slippage=source_report["config"]["slippage"],
                    ),
                )
                result = simulate(
                    local,
                    clock,
                    "wyckoff_60f_v31",
                    cfg,
                    market_policy=MarketPolicy("none"),
                    wyckoff_policy=protection,
                    wyckoff_families=("base_rebound",),
                    wyckoff_slot_limit=settings["reversal_slots"],
                    first_session_stop_review=review,
                )
                result = with_benchmarks(result, normal["equity"])
                m = result["metrics"]
                m["worst_trade_return_pct"] = min(
                    (t["return_pct"] for t in result["trades"]), default=None
                )
                if case == "control" and window == "full":
                    for key in (
                        "metrics",
                        "trades",
                        "equity",
                        "open_positions",
                        "execution_audit",
                        "config",
                    ):
                        if json.loads(json.dumps(result[key])) != source_report[key]:
                            raise AssertionError(f"v40对照已改变: {key}")
                label = window + ("__stress" if is_stress else "")
                details = dict(m)
                if window == "full":
                    starting = next(
                        x["equity"]
                        for x in result["equity"]
                        if x["date"] == prior["cutoff"][:10]
                    )
                    details["last_four_sessions_return_pct"] = (
                        m["ending_equity"] / starting - 1
                    ) * 100
                result.update(
                    run_id=str(uuid4()),
                    suite_id=suite,
                    strategy_id="wyckoff_60f_v31",
                    strategy_version=VERSION,
                    strategy_config=normal["strategy_config"],
                    timeframe="1D",
                    universe_mode="fixed_snapshot",
                    start=start,
                    end=end,
                    diagnostics=dict(
                        research_kind="wyckoff",
                        research_variant=f"{case}:{label}",
                        eligible_for_default=False,
                        minimum_quality_score=72,
                        independent_validation=False,
                        warnings=declared["warnings"],
                    ),
                )
                write_result(folder / case / label, result)
                summary[case][label] = details
                logger.info(
                    "%s %s net=%.4f%% drawdown=%.4f%%",
                    case,
                    label,
                    m["total_return_pct"],
                    m["max_drawdown_pct"],
                )
    write_json(folder / "comparison.json", summary)


if __name__ == "__main__":
    main()
