"""Frozen v22: native quality >=72, monthly priority and bounded exit research.

Uses local v13/v21 artifacts only. Declare before development runs; select on
Jan–Jun normal AND stressed costs before replaying the later known windows.
"""

import argparse
from dataclasses import fields, replace
import json
from pathlib import Path
import pickle
from uuid import uuid4

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from workflows.quant.studies.provenance import digest, freeze, source_hashes
from workflows.quant.backtests.mtf import WINDOWS, window_series
from workflows.quant.studies.mainboard_60f import qualified, score, write_json
from skills.quant.candidate_quality import attach_signal_quality
from skills.quant.market_context import MarketPolicy, apply_policy
from skills.quant.models import BacktestConfig
from skills.quant.report import with_benchmarks, write_result
from skills.quant.tranche_engine import simulate
from skills.quant.tranches import TrancheConfig
from skills.quant.support_protection import SupportProtection
from skills.quant.stop_orders import IntradayProtection

logger = get_logger(__name__)
VERSION = "quality72-exits-v22"
FOLDER = "quality72_exits_v22"
MINIMUM_QUALITY = 72
ORIGINAL = Path("output/quant/market_priority_v13/prepared.pkl")
RANKED = Path("output/quant/online_priority_v21/prepared.pkl")
BASELINE = Path("output/quant/online_priority_v21/risk2.5_532_protect5/full")
STRESS = Path("output/quant/online_priority_v21/selected_cost_stress")
# Each case tests earlier protection, wave holding or a tighter fill risk cap.
# Account risk, position limits, entry model and 532 acquisition stay fixed.
CASES = {
    "baseline": {},
    "protect3": {"protect_at_pct": 3.0},
    "protect2.5": {"protect_at_pct": 2.5},
    "wave6_10": {"protect_at_pct": 3.0, "take_profit_pct": (6.0, 10.0)},
    "wave30": {
        "protect_at_pct": 3.0,
        "take_profit_pct": (6.0, 10.0),
        "max_holding_bars": 30,
    },
    "risk2": {"protect_at_pct": 3.0, "max_entry_risk_pct": 2.0},
    "risk2_wave": {
        "protect_at_pct": 3.0,
        "max_entry_risk_pct": 2.0,
        "take_profit_pct": (6.0, 10.0),
    },
    "sell442": {"protect_at_pct": 3.0, "exit_weights": (0.4, 0.4, 0.2)},
}


def read_report(folder):
    paths = list(folder.glob("1D_*.json"))
    if len(paths) != 1:
        raise ValueError(f"Expected exactly one frozen report: {folder}")
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def config_for(base, name, stress=False, cases=CASES):
    changes = {
        k: v
        for k, v in cases[name].items()
        if k
        not in (
            "preplaced_stops",
            "market_mode",
            "support_protection",
            "intraday_protection",
        )
    }
    base_keys = {f.name for f in fields(BacktestConfig)}
    base = replace(
        base,
        base=replace(
            base.base,
            **{k: v for k, v in changes.items() if k in base_keys},
            slippage=0.001 if stress else 0.0005,
        ),
    )
    return replace(base, **{k: v for k, v in changes.items() if k not in base_keys})


def configuration(report):
    values = report["config"]
    # The report flattens the outer tranche policy over the base policy.
    base_values = {f.name: values[f.name] for f in fields(BacktestConfig)}
    base_values["exit_policy"] = "legacy"
    base = BacktestConfig(**base_values)
    return TrancheConfig(
        base=base,
        **{
            f.name: tuple(values[f.name])
            if isinstance(values[f.name], list)
            else values[f.name]
            for f in fields(TrancheConfig)
            if f.name != "base"
        },
    )


def run(
    root,
    folder,
    declared,
    baseline,
    prior_stress,
    cases=CASES,
    version=VERSION,
    stop_bars=None,
    transform=None,
    model_variants=None,
):
    with (root / ORIGINAL).open("rb") as stream:
        originals = pickle.load(stream)["series"]
    with (root / RANKED).open("rb") as stream:
        payload = pickle.load(stream)
    stocks = attach_signal_quality(payload["series"], originals, MINIMUM_QUALITY)
    if transform is not None:
        stocks = transform(stocks)
    signals = [signal for stock in stocks for signal in stock.signals]
    model_causality = all(
        not m["trained"]
        or pd.Timestamp(m["last_label"]) < pd.Period(m["month"]).start_time
        for m in payload["models"]
    )
    if not signals or not model_causality:
        raise ValueError("Missing qualified candidates or noncausal frozen model")
    write_json(
        folder / "quality_audit.json",
        dict(
            stocks=len(stocks),
            source_signals=sum(len(s.signals) for s in payload["series"]),
            retained_signals=len(signals),
            minimum_quality=min(s["quality_score"] for s in signals),
            maximum_quality=max(s["quality_score"] for s in signals),
            quality_scale="v8-native-0-120",
            model_labels_before_training_month=model_causality,
            prediction_range=[
                min(s["priority_score"] for s in signals),
                max(s["priority_score"] for s in signals),
            ],
        ),
    )
    del originals, payload
    calendar = pd.DatetimeIndex(
        [pd.Timestamp(p["date"]) + pd.Timedelta(hours=15) for p in baseline["equity"]]
    )
    plan, suite = configuration(baseline), str(uuid4())

    def calculate(name, window, stress=False):
        start, end = WINDOWS[window]
        cfg = config_for(plan, name, stress, cases)
        variant = (model_variants or {}).get(name)
        source = window_series(variant["series"] if variant else stocks, start, end)
        market_policy = MarketPolicy(cases[name].get("market_mode", "none"))
        if market_policy.entry_mode != "none":
            source = [apply_policy(stock, market_policy) for stock in source]
        clock = calendar[
            (calendar >= start) & (calendar < pd.Timestamp(end) + pd.Timedelta(days=1))
        ]
        result = simulate(
            source,
            clock,
            "swing_60f_v8",
            cfg,
            market_policy=market_policy,
            stop_bars=stop_bars if cases[name].get("preplaced_stops") else None,
            support_protection=SupportProtection(**cases[name]["support_protection"])
            if "support_protection" in cases[name]
            else None,
            intraday_protection=IntradayProtection(**cases[name]["intraday_protection"])
            if "intraday_protection" in cases[name]
            else None,
        )
        result["metrics"]["worst_trade_return_pct"] = min(
            (t["return_pct"] for t in result["trades"]), default=None
        )
        accounted = (
            cfg.base.initial_cash
            + sum(t["pnl"] for t in result["trades"])
            + sum(
                p["unrealized_pnl"] + p["partial_realized_pnl"]
                for p in result["open_positions"]
            )
        )
        if abs(accounted - result["metrics"]["ending_equity"]) > 1e-5:
            raise AssertionError("Portfolio cash/campaign accounting mismatch")
        result.update(
            run_id=str(uuid4()),
            suite_id=suite,
            strategy_id="swing_60f_v8",
            strategy_version=version,
            strategy_config={
                **baseline["strategy_config"],
                **({"online": variant["model_config"]} if variant else {}),
                "minimum_native_quality": MINIMUM_QUALITY,
            },
            timeframe="1D",
            universe_mode="fixed_snapshot",
            start=start,
            end=end,
            diagnostics=dict(
                research_kind="online_market",
                research_variant=name + ("__cost_stress" if stress else ""),
                in_sample=True,
                independent_validation=False,
                eligible_for_default=False,
                research_selected=False,
                research_status="experimental",
                universe_size=len(stocks),
                usable_stocks=len(source),
                bar_count=sum(len(s.times) for s in source),
                trend_timeframe="60F",
                entry_timeframe="1D",
                execution_schedule="after_close",
                minimum_quality_score=MINIMUM_QUALITY,
                quality_scale="v8-native-0-120",
                warnings=list(declared["warnings"]),
            ),
        )
        logger.info(
            "%s %s stress=%s return=%.4f%% worst=%s",
            name,
            window,
            stress,
            result["metrics"]["total_return_pct"],
            result["metrics"]["worst_trade_return_pct"],
        )
        return with_benchmarks(result, baseline["equity"])

    for stressed_cost, prior in {False: baseline, True: prior_stress}.items():
        replay = calculate("baseline", "full", stressed_cost)
        for key in ("metrics", "equity", "config", "execution_audit", "open_positions"):
            if json.loads(json.dumps(replay[key])) != prior[key]:
                raise AssertionError(f"Frozen baseline parity failed: {key}")
        trades = [{**t, "entry_evidence": None} for t in replay["trades"]]
        if trades != prior["trades"]:
            raise AssertionError("Frozen baseline trades changed")
    write_json(
        folder / "baseline_parity.json",
        dict(normal=True, stress=True, trades=True, accounting=True),
    )

    development, stressed, eligible = {}, {}, []
    for name in cases:
        development[name] = calculate(name, "development")
        stressed[name] = calculate(name, "development", True)
        for label, report in (
            ("normal", development[name]),
            ("stress", stressed[name]),
        ):
            write_result(folder / "development" / name / label, report)
        if qualified(development[name]) and qualified(stressed[name]):
            eligible.append(name)
    selected = (
        max(
            eligible,
            key=lambda n: min(
                score(development[n]["metrics"]), score(stressed[n]["metrics"])
            ),
        )
        if eligible
        else None
    )
    freeze(
        folder / "selection.json",
        dict(
            selected=selected,
            eligible=eligible,
            selection_window="development",
            normal={n: r["metrics"] for n, r in development.items()},
            stress={n: r["metrics"] for n, r in stressed.items()},
        ),
    )
    if selected is None:
        write_json(
            folder / "verification.json",
            dict(
                historical_target_met=False,
                reason="No development candidate passed both cost scenarios",
            ),
        )
        return
    results = {}
    for window in ("validation", "time_holdout", "full"):
        for stress in (False, True):
            label = window + ("__stress" if stress else "")
            report = calculate(selected, window, stress)
            report["diagnostics"]["research_selected"] = True
            results[label] = report
            write_result(folder / selected / label, report)
    normal, stress = results["full"]["metrics"], results["full__stress"]["metrics"]
    passed = normal["total_return_pct"] >= 8 and all(
        m["max_drawdown_pct"] <= 8
        and m["closed_trades"] >= 30
        and m["worst_trade_return_pct"] >= -8
        and m["total_return_pct"] > 0
        for m in (normal, stress)
    )
    summary = dict(
        selected=selected,
        target_return_pct=8,
        historical_target_met=passed,
        independent_validation=False,
        eligible_for_default=False,
        minimum_quality=MINIMUM_QUALITY,
        quality_scale="v8-native-0-120",
        full=normal,
        cost_stress=stress,
        windows={n: r["metrics"] for n, r in results.items()},
        unchanged_risk=dict(
            account_pct=plan.base.risk_budget_pct,
            max_stock_pct=plan.base.position_weight * 100,
            max_positions=plan.base.max_positions,
        ),
    )
    write_json(folder / "comparison.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    folder = root / "output/quant" / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    baseline_path, baseline = read_report(root / BASELINE)
    stress_path, stress = read_report(root / STRESS)
    paths = [ORIGINAL, RANKED, baseline_path, stress_path]
    code_paths = [
        "workflows/quant/studies/quality_60f.py",
        "skills/quant/candidate_quality.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranches.py",
        "skills/quant/models.py",
        "workflows/quant/backtests/mtf.py",
        "workflows/quant/studies/mainboard_60f.py",
        "skills/quant/report.py",
    ]
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=VERSION,
            minimum_quality_score=MINIMUM_QUALITY,
            cases=CASES,
            windows=WINDOWS,
            input_sha256={
                str(Path(p).relative_to(root) if Path(p).is_absolute() else p): digest(
                    root / p
                )
                for p in paths
            },
            code_sha256=source_hashes(root, code_paths),
            selection="Jan-Jun only: normal and doubled-slippage positive/PF>1/>=30 campaigns/MDD<=8%/worst>=-8%; maximize minimum return/max(MDD,1). Freeze choice before later windows.",
            target="Full Jan1-Nov12 funded net return>=8%; >=30 campaigns, MDD<=8%, worst>=-8% under BOTH costs, stressed return>0. No leverage or increased account risk.",
            warnings=[
                "Repeatedly inspected 2026 research, NOT independent out-of-sample validation or a live recommendation.",
                "2333 current non-ST mainboard stocks from a fixed five-index union; current membership/survivorship bias.",
                "V8 native quality is 0-120; gate72 is applied before portfolio priority. It is not the frontend's separately defined historical V7 score or a success probability.",
                "Monthly model uses only completed prior labels. Jan-Mar warmup cash remains in full-period returns.",
                "Daily bars supply execution/valuation only; triggers remain completed60F. Next legal open, real limits, T+1, fees and minimum commissions apply.",
                "Planned2-2.5% stop distance/.35% account risk do not cap overnight/limit-gap losses. Factor-share accounting is an approximation.",
                "No automatic default changes or broker orders. Model, training and original setup formulas remain frozen.",
            ],
        ),
        source_root=root,
    )
    if args.declare_only:
        print(
            "Declared8 fixed exit cases and quality72 gate; no performance selection yet."
        )
        return
    run(root, folder, declared, baseline, stress)


if __name__ == "__main__":
    main()
