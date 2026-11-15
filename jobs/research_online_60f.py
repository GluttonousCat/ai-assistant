"""Replay the declared monthly index-relative 60F model with stricter staging.

All inputs are frozen local research artifacts; no HTTP-request-time fitting,
external fetching, default-strategy changes or broker orders occur here.
"""

import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import pickle
from uuid import uuid4

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from jobs.backtest_mtf import WINDOWS, window_series
from jobs.research_mainboard_60f import ENTRY_PROFILES, qualified, score, write_json
from jobs.research_market_context import PLAN, REFERENCE, load_indices
from skills.quant.market_context import MarketPolicy, apply_policy
from skills.quant.online_priority import (
    OnlineConfig,
    VERSION,
    FEATURES,
    augment_features,
    training_rows,
    fit_monthly,
    apply_predictions,
)
from skills.quant.report import write_result, with_benchmarks
from skills.quant.tranche_engine import simulate

logger = get_logger(__name__)
MODEL_CFG = OnlineConfig()
FOLDER = "online_priority_v21"
CASES = {
    "control": (3.0, "532", 3.0),
    **{
        f"risk{risk:g}_{weight}_protect{protect:g}": (risk, weight, protect)
        for risk in (1.5, 2.0, 2.5)
        for weight in ("442", "532")
        for protect in (3.0, 5.0)
    },
}


def declare(root, folder, index_audit):
    paths = [
        "skills/quant/online_priority.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranches.py",
        "skills/quant/market_context.py",
        "jobs/research_online_60f.py",
    ]
    source = root / "output/quant/market_priority_v13/prepared.pkl"
    value = json.loads(
        json.dumps(
            dict(
                version=VERSION,
                model_config=asdict(MODEL_CFG),
                features=FEATURES,
                cases=CASES,
                windows=WINDOWS,
                indices=index_audit,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                code_sha256={
                    p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                    for p in paths
                },
                entry_config=asdict(ENTRY_PROFILES["balanced"]),
                model="Monthly expanding ridge100 on known beta-adjusted auxiliary outcomes; >=500 historical outcomes, cash until ready. Every label exit precedes training month; no current label eligibility influences prediction. Standardization/clipping use training rows only.",
                selection="Jan-Jun only, positive return/PF>1/>=30 campaigns/MDD<=8%/worst>=-8%; maximize return/max(MDD,1). Known-failed3% control excluded. All2026 already seen; not independent validation.",
                target="Full2026>=5%, MDD<=8%, >=30 campaigns, worst normal campaign>=-8%, doubled-slippage positive. Stress tail loss is reported separately, not silently bounded to8%.",
                history="v11-v14 88 cases all failed. v15-v17 causal models failed. v18 full6.39% and v19 full8.05% failed worst-campaign gate. v20 only tightened entry/add cap to<=2.5%, kept .35% account risk/10% stock limit; v21 is a parity-checked formal implementation, no new hyperparameters.",
                warnings=[
                    "All2026, including later windows and known examples, informed repeated research. Historical threshold success is not independent validation or a live recommendation.",
                    "2333 current non-ST mainboard constituents of a fixed five-index union; current membership/survivorship bias remains.",
                    "Cash during model warmup is included in the full Jan1-Nov12 portfolio. At least500 overlapping historical labels do not mean500 independent observations.",
                    "Individual shapes and predictors use60F only; daily bars supply execution/outcome prices and liquidity. No daily candle-shape entry gate.",
                    "Auxiliary labels use one-entry10-session close triggers, next legal open and proportional fees. Actual532 portfolio uses real limits, T+1, minimum fees, staged exits and fixed original stops.",
                    "Planned2.5% entry/add distance and .35% account risk are not guaranteed loss caps. Overnight gaps/limits can produce larger losses, including in cost stress.",
                    "Factor-share approximation does not reconstruct a cash corporate-action ledger; exit splitting/market impact is not fully modeled.",
                ],
            )
        )
    )
    path = folder / "declaration.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
        raise ValueError("Frozen online experiment changed; use a new explicit version")
    if not path.exists():
        write_json(path, value)
        for p in paths:
            target = folder / "source" / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / p).read_bytes())
    return value


def prepare(root, folder, frames, calendar):
    with (root / "output/quant/market_priority_v13/prepared.pkl").open("rb") as stream:
        original = pickle.load(stream)
    stocks = []
    for n, stock in enumerate(original["series"], 1):
        minute = pd.read_pickle(
            root / "output/quant/mainboard_60f_v8/minutes" / f"{stock.code}.pkl"
        )
        stocks.append(augment_features(minute, stock))
        if n % 500 == 0 or n == len(original["series"]):
            logger.info("Online features %s/%s", n, len(original["series"]))
    records = training_rows(stocks, calendar, frames, MODEL_CFG)
    models, predictions = fit_monthly(records, MODEL_CFG)
    write_json(folder / "models.json", models)
    selected = [
        apply_policy(s, MarketPolicy("none"))
        for s in apply_predictions(stocks, predictions, MODEL_CFG)
    ]
    # Compare by code and signal identity, never positional multi-source joins.
    with (root / "output/quant/online_priority_v19/prepared.pkl").open("rb") as stream:
        frozen = {s.code: s for s in pickle.load(stream)["series"]}
    assert set(frozen) == {s.code for s in selected}
    for stock in selected:
        a = {(s["i"], s["leg"]): s for s in stock.signals}
        b = {(s["i"], s["leg"]): s for s in frozen[stock.code].signals}
        assert set(a) == set(b), stock.code
        for key in a:
            assert abs(a[key]["score"] - b[key]["score"]) < 1e-10
            assert a[key]["stop"] == b[key]["stop"]
    payload = dict(
        series=selected, models=models, universe_size=original["universe_size"]
    )
    with (folder / "prepared.pkl").open("wb") as stream:
        pickle.dump(payload, stream, pickle.HIGHEST_PROTOCOL)
    write_json(
        folder / "model_parity.json",
        dict(
            signal_identity=True,
            scores_atol=1e-10,
            stocks=len(selected),
            signals=sum(len(s.signals) for s in selected),
            every_training_label_before_month=all(
                not m["trained"]
                or pd.Timestamp(m["last_label"]) < pd.Period(m["month"]).start_time
                for m in models
            ),
        ),
    )
    return payload


def run(root, folder, payload, declared, calendar):
    suite = str(uuid4())
    reference = json.loads((root / REFERENCE).read_text(encoding="utf-8"))["equity"]

    def calculate(name, window, stress=False):
        risk, weights, protection = CASES[name]
        cfg = replace(
            PLAN,
            base=replace(
                PLAN.base, max_entry_risk_pct=risk, slippage=0.001 if stress else 0.0005
            ),
            entry_weights=(0.5, 0.3, 0.2) if weights == "532" else PLAN.entry_weights,
            exit_weights=(0.5, 0.3, 0.2) if weights == "532" else PLAN.exit_weights,
            protect_at_pct=protection,
        )
        start, end = WINDOWS[window]
        source = window_series(payload["series"], start, end)
        clock = calendar[
            (calendar >= start) & (calendar < pd.Timestamp(end) + pd.Timedelta(days=1))
        ]
        r = simulate(
            source, clock, "swing_60f_v8", cfg, market_policy=MarketPolicy("none")
        )
        r.update(
            run_id=str(uuid4()),
            suite_id=suite,
            strategy_id="swing_60f_v8",
            strategy_version=VERSION,
            strategy_config=dict(
                base_entry=asdict(ENTRY_PROFILES["balanced"]), online=asdict(MODEL_CFG)
            ),
            timeframe="1D",
            universe_mode="fixed_snapshot",
            start=start,
            end=end,
            diagnostics=dict(
                research_kind="online_market",
                research_variant=name + ("__cost_stress" if stress else ""),
                candidate_count=len(CASES),
                in_sample=True,
                research_selected=False,
                eligible_for_default=False,
                research_status="experimental",
                warnings=list(declared["warnings"]),
                universe_size=payload["universe_size"],
                usable_stocks=len(source),
                bar_count=sum(len(s.times) for s in source),
                trend_timeframe="60F",
                entry_timeframe="1D",
                execution_schedule="after_close",
                exit_profile="entry_exit_" + weights,
                model_min_samples=MODEL_CFG.min_samples,
                model_target="market-relative completed auxiliary outcomes",
                model_update="monthly, past labels only",
            ),
        )
        r["metrics"]["worst_trade_return_pct"] = min(
            (t["return_pct"] for t in r["trades"]), default=None
        )
        value = (
            cfg.base.initial_cash
            + sum(t["pnl"] for t in r["trades"])
            + sum(
                p["unrealized_pnl"] + p.get("partial_realized_pnl", 0)
                for p in r["open_positions"]
            )
        )
        assert abs(r["metrics"]["ending_equity"] - value) < 1e-5
        return with_benchmarks(r, reference)

    train = {}
    for name in CASES:
        r = calculate(name, "development")
        train[name] = r
        write_result(folder / "development" / name, r)
    eligible = [n for n, r in train.items() if n != "control" and qualified(r)]
    selected = (
        max(eligible, key=lambda n: score(train[n]["metrics"])) if eligible else None
    )
    write_json(
        folder / "selection.json",
        dict(
            selected=selected,
            qualified=bool(eligible),
            development={n: r["metrics"] for n, r in train.items()},
        ),
    )
    if selected is None:
        raise RuntimeError(
            "No qualified development candidate; retain existing default"
        )
    result = {}
    for window in ("validation", "time_holdout", "full"):
        r = calculate(selected, window)
        r["diagnostics"]["selected_on_development"] = True
        result[window] = r
        write_result(folder / selected / window, r)
    stress = calculate(selected, "full", True)
    control = calculate("control", "full")
    full = result["full"]
    prior_path = next(
        (root / "output/quant/online_priority_v20" / selected / "full").glob(
            "1D_*.json"
        )
    )
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    for key in (
        "metrics",
        "trades",
        "equity",
        "open_positions",
        "config",
        "execution_audit",
    ):
        assert json.loads(json.dumps(full[key])) == prior[key], key
    m = full["metrics"]
    passed = bool(
        m["total_return_pct"] >= 5
        and m["max_drawdown_pct"] <= 8
        and m["closed_trades"] >= 30
        and m["worst_trade_return_pct"] >= -8
        and stress["metrics"]["total_return_pct"] > 0
    )
    for r in (full, stress, control):
        r["diagnostics"]["historical_target_met"] = (
            passed if r is not control else False
        )
        r["diagnostics"]["warnings"].append(
            f"本行最差完整持仓周期净收益{r['metrics']['worst_trade_return_pct']:.4f}%；计划风险不等于实现损失上限。"
        )
    write_result(folder / selected / "full", full)
    write_result(folder / "selected_cost_stress", stress)
    write_result(folder / "control" / "full", control)
    summary = dict(
        selected=selected,
        historical_target_met=passed,
        independent_validation=False,
        full=m,
        cost_stress=stress["metrics"],
        windows={w: r["metrics"] for w, r in result.items()},
        first_entry=min(
            [t["entry_time"] for t in full["trades"]]
            + [p["entry_time"] for p in full["open_positions"]]
        ),
        unchanged_risk=dict(account_pct=0.35, max_stock_pct=10, max_positions=10),
        formal_model_and_portfolio_parity=True,
    )
    write_json(folder / "comparison.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--publish-only", action="store_true")
    parser.add_argument(
        "--publish-variant", choices=["selected", "selected_cost_stress", "control"]
    )
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    folder = root / "output/quant" / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    if args.publish_only:
        from jobs.backtest_quant import _database_unit
        from storage.repositories.backtest import publish
        from storage.schema.quant import ensure_quant_schema

        summary = json.loads((folder / "comparison.json").read_text(encoding="utf-8"))
        files = {
            "selected": next((folder / summary["selected"] / "full").glob("1D_*.json")),
            "selected_cost_stress": next(
                (folder / "selected_cost_stress").glob("1D_*.json")
            ),
            "control": next((folder / "control" / "full").glob("1D_*.json")),
        }
        ensure_quant_schema()
        for name in [args.publish_variant] if args.publish_variant else files:
            report = json.loads(files[name].read_text(encoding="utf-8"))
            _database_unit(lambda: publish(report, trade_page_size=25))
            logger.info("Published online research %s", name)
        print("Published online research; defaults unchanged.")
        return
    if args.publish_variant:
        parser.error("--publish-variant requires --publish-only")
    frames, calendar, audit = load_indices(root / "output/quant/market_v11", root)
    declared = declare(root, folder, audit)
    if args.declare_only:
        print("Frozen formal model and13 staging cases; no new model selection.")
        return
    if args.resume:
        with (folder / "prepared.pkl").open("rb") as stream:
            payload = pickle.load(stream)
    else:
        payload = prepare(root, folder, frames, calendar)
    run(root, folder, payload, declared, calendar)


if __name__ == "__main__":
    main()
