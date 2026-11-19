"""Replay frozen portfolios continuously into four newly captured sessions."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import pickle
from uuid import uuid4

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from workflows.quant.studies.provenance import digest, freeze, source_hashes
from workflows.quant.studies.mainboard_60f import ENTRY_PROFILES, write_json
from workflows.quant.studies.market_context import load_indices
from workflows.quant.studies.quality_60f import (
    BASELINE,
    configuration,
    read_report,
)
from skills.quant.candidate_quality import attach_signal_quality
from skills.quant.forward_replay import (
    append_evidence,
    extend_execution,
    frozen_predictions,
)
from skills.quant.market_context import (
    MarketPolicy,
    apply_policy,
    attach_market,
    index_context,
)
from skills.quant.online_priority import apply_predictions, augment_features
from skills.quant.prepare import prepare_swing
from skills.quant.report import with_benchmarks, write_result
from skills.quant.signal_priority import attach_priority
from skills.quant.support_protection import SupportProtection
from skills.quant.tranche_engine import simulate
from skills.quant.tranche_prepare import (
    attach_confirmations,
    attach_entry_session_low,
    confirm_signal_close,
)
from skills.quant.wyckoff import prepare_wyckoff
from skills.quant.wyckoff_hybrid import combine_families
from skills.trend.wyckoff import WyckoffConfig

logger = get_logger(__name__)
VERSION = "wyckoff-forward-20261118-v40"
CUTOFF = pd.Timestamp("2026-11-12T15:00:00")
DATES = ("2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18")


def load_csv(folder, coverage, name):
    path = folder / f"{name}.csv"
    if digest(path) != coverage["sha256"][name]:
        raise ValueError(f"冻结输入校验失败: {name}")
    return pd.read_csv(path, float_precision="round_trip")


def assemble(root, inputs, coverage, declared, index_frames):
    forward_daily = load_csv(inputs, coverage, "quotes_1D")
    forward_minute = load_csv(inputs, coverage, "quotes_60F").assign(
        adjustment="none",
        interval_minutes=60,
        source="akshare-sina",
    )
    for frame in (forward_daily, forward_minute):
        frame["bar_end"] = pd.to_datetime(frame.bar_end)
        if frame.bar_end.min() <= CUTOFF or frame.bar_end.max() > pd.Timestamp(
            DATES[-1] + "T15:00:00"
        ):
            raise ValueError("新增行情越过声明区间")
    daily = dict(tuple(forward_daily.groupby("ts_code")))
    minute = dict(tuple(forward_minute.groupby("ts_code")))
    anchors = load_csv(inputs, coverage, "factor_anchors").set_index("ts_code")
    if (
        set(anchors.index) != set(daily)
        or set(minute) != set(daily)
        or not anchors.index.is_unique
    ):
        raise ValueError("新增行情股池和复权锚点不匹配")
    with (root / declared["stock_cache"]).open("rb") as stream:
        frozen = pickle.load(stream)
    cache = root / "output/quant/mainboard_60f_v8/minutes"
    volume = {s.code: s for s in frozen["price_volume_primary"]}
    market, ret, ret4 = index_context(index_frames)
    ret20 = pd.concat(
        [
            frame.set_index("bar_end").close.pct_change(20, fill_method=None)
            for frame in index_frames.values()
        ],
        axis=1,
    ).mean(axis=1, skipna=False)
    volume_cfg = WyckoffConfig(**declared["volume_config"])
    ranked_all, volume_all, review_lows = [], [], {}
    historical_checks = 0
    for count, stock in enumerate(frozen["baseline"], 1):
        pv = volume[stock.code]
        old_minutes = pd.read_pickle(cache / f"{stock.code}.pkl")
        if stock.code in daily:
            history = pd.concat(
                [old_minutes, minute[stock.code][old_minutes.columns]],
                ignore_index=True,
            )
            execution = extend_execution(
                stock, daily[stock.code], anchors.loc[stock.code]
            )
            swing, issue = prepare_swing(history, execution, ENTRY_PROFILES["balanced"])
            if issue:
                raise ValueError(f"{stock.code}: {issue}")
            swing = attach_confirmations(history, swing)
            swing = confirm_signal_close(history, swing)
            swing = attach_market(history, swing, market, ret, ret4)
            swing = attach_priority(history, swing, ret20)
            swing = augment_features(history, swing)
            candidate = replace(
                swing,
                signals=[s for s in swing.signals if swing.times[s["i"]].month == 11],
            )
            forecast = frozen_predictions(candidate, declared["november_model"])
            ranked = apply_policy(
                apply_predictions([candidate], forecast)[0], MarketPolicy("none")
            )
            ranked = attach_signal_quality([ranked], [candidate], minimum_score=72)[0]
            expected = {
                (s["i"], s["leg"]): s
                for s in stock.signals
                if stock.times[s["i"]].month == 11
            }
            replayed = {
                (s["i"], s["leg"]): s
                for s in ranked.signals
                if s["i"] < len(stock.times)
            }
            if set(expected) != set(replayed) or any(
                abs(expected[k]["score"] - replayed[k]["score"]) > 1e-9
                or expected[k]["stop"] != replayed[k]["stop"]
                for k in expected
            ):
                raise AssertionError(f"{stock.code} 11月冻结模型前缀不一致")
            historical_checks += len(expected)
            stock = append_evidence(stock, ranked)
            pv = append_evidence(
                pv, prepare_wyckoff(history, swing, volume_cfg, after=CUTOFF)
            )
        else:
            history = old_minutes
        if any(s["family"] == "trend_pullback" for s in stock.signals):
            review_lows[stock.code] = attach_entry_session_low(history, stock).data[
                "entry_session_60f_low"
            ]
        ranked_all.append(stock)
        volume_all.append(pv)
        if count % 250 == 0 or count == len(frozen["baseline"]):
            logger.info("Forward evidence %s/%s", count, len(frozen["baseline"]))
    mixed = [
        replace(s, data={**s.data, "entry_session_60f_low": review_lows[s.code]})
        if s.code in review_lows
        else s
        for s in combine_families(ranked_all, volume_all)
    ]
    return dict(baseline=ranked_all, hybrid=mixed, historical_checks=historical_checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    inputs = root / "output/quant/forward_inputs_20261118"
    folder = root / "output/quant/wyckoff_forward_v40"
    folder.mkdir(parents=True, exist_ok=True)
    v33 = root / "output/quant/wyckoff_price_volume_v33"
    v38 = root / "output/quant/wyckoff_family_v38"
    index_root = root / "output/quant/market_v11"
    coverage = json.loads((inputs / "coverage.json").read_text(encoding="utf-8"))
    prior = json.loads((v33 / "declaration.json").read_text(encoding="utf-8"))
    model_path = root / "output/quant/online_priority_v21/models.json"
    model = next(
        m
        for m in json.loads(model_path.read_text(encoding="utf-8"))
        if m["month"] == "2026-11"
    )
    baseline_path, baseline = read_report(root / BASELINE)
    policy = {"baseline": "baseline", "hybrid": "family_hybrid_first_session_reduce"}
    prefixes = {
        (name, label): read_report(v38 / case / label)
        for name, case in policy.items()
        for label in ("full", "full__stress")
    }
    historical_indices, calendar, audit = load_indices(index_root, root)
    reference = list(baseline["equity"])
    new_benchmarks = {day: {} for day in DATES}
    frames = {}
    for code, old in historical_indices.items():
        incoming = load_csv(inputs, coverage, f"index_60F_{code}")
        incoming["bar_end"] = pd.to_datetime(incoming.bar_end)
        daily = (
            load_csv(inputs, coverage, f"index_1D_{code}").set_index("trade_date").close
        )
        final = old.loc[old.bar_end.eq(CUTOFF), "close"].item()
        index_base = reference[-1]["benchmarks"][code] / final
        for day in DATES:
            new_benchmarks[day][code] = (
                float(daily.loc[int(day.replace("-", ""))]) * index_base
            )
        frames[code] = pd.concat([old, incoming[old.columns]], ignore_index=True)
    reference.extend(dict(date=day, benchmarks=new_benchmarks[day]) for day in DATES)
    calendar = calendar.append(
        pd.DatetimeIndex([pd.Timestamp(day) + pd.Timedelta(hours=15) for day in DATES])
    )
    source_paths = [
        v33 / "prepared.pkl",
        v33 / "declaration.json",
        model_path,
        baseline_path,
        inputs / "coverage.json",
        *(path for path, _ in prefixes.values()),
    ]
    code = [
        "workflows/quant/studies/wyckoff/forward.py",
        "skills/quant/forward_replay.py",
        "skills/quant/wyckoff.py",
        "skills/trend/wyckoff.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/tranche_prepare.py",
        "skills/quant/market_context.py",
        "skills/quant/online_priority.py",
        "skills/quant/prepare.py",
        "skills/quant/signal_priority.py",
        "skills/quant/wyckoff_hybrid.py",
        "skills/trend/swing.py",
        "skills/trend/mtf.py",
        "skills/quant/tranches.py",
        "skills/quant/support_protection.py",
        "skills/quant/models.py",
        "skills/quant/report.py",
        "skills/quant/metrics.py",
        "workflows/quant/studies/mainboard_60f.py",
        "workflows/quant/studies/quality_60f.py",
    ]
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=VERSION,
            cutoff=CUTOFF.isoformat(),
            forward_dates=DATES,
            minimum_quality_score=72,
            stock_cache=str((v33 / "prepared.pkl").relative_to(root)),
            november_model=model,
            volume_config=prior["config"],
            protection=prior["protection"],
            code_sha256=source_hashes(root, code),
            inputs={str(p.relative_to(root)): digest(p) for p in source_paths},
            index_prefix=audit,
            forward_quotes=coverage["sha256"],
            warnings=[
                "Fixed November model with training outcomes known by October 30. No refit or parameter selection.",
                "Replay from January preserves the original cash and open inventories at November 12. Four forward sessions are descriptive and short.",
                "Frozen historical fixed-universe membership bias remains. Benchmarks extend from their last frozen point using official new closes on the native index scale.",
            ],
        ),
        source_root=root,
    )
    cache = folder / "prepared.pkl"
    if args.resume:
        with cache.open("rb") as stream:
            data = pickle.load(stream)
    else:
        data = assemble(root, inputs, coverage, declared, frames)
        pending = cache.with_suffix(".tmp")
        with pending.open("wb") as stream:
            pickle.dump(data, stream, pickle.HIGHEST_PROTOCOL)
        pending.replace(cache)
    plan = configuration(baseline)
    suite = str(uuid4())
    summary = {}
    for name in policy:
        for label, slip in (("full", 0.0005), ("full__stress", 0.001)):
            mixed = name == "hybrid"
            strategy = "wyckoff_60f_v31" if mixed else "swing_60f_v8"
            result = simulate(
                data[name],
                calendar,
                strategy,
                replace(plan, base=replace(plan.base, slippage=slip)),
                market_policy=MarketPolicy("none"),
                wyckoff_policy=SupportProtection(**prior["protection"])
                if mixed
                else None,
                wyckoff_families=("base_rebound",) if mixed else None,
                wyckoff_slot_limit=2 if mixed else None,
                first_session_stop_review="reduce" if mixed else None,
            )
            result = with_benchmarks(result, reference)
            _, frozen = prefixes[name, label]
            n = len(frozen["equity"])
            if (
                result["equity"][:n] != frozen["equity"]
                or [
                    t
                    for t in result["trades"]
                    if pd.Timestamp(t["exit_time"]) <= CUTOFF
                ]
                != frozen["trades"]
            ):
                raise AssertionError(f"{name} {label} 冻结期逐日净值或成交不一致")
            result["metrics"]["worst_trade_return_pct"] = min(
                t["return_pct"] for t in result["trades"]
            )
            result.update(
                run_id=str(uuid4()),
                suite_id=suite,
                strategy_id=strategy,
                strategy_version=VERSION,
                strategy_config=frozen["strategy_config"],
                timeframe="1D",
                universe_mode="fixed_snapshot",
                start=frozen["start"],
                end=DATES[-1],
                diagnostics=dict(
                    research_kind="wyckoff",
                    research_variant=name + ":" + label,
                    eligible_for_default=False,
                    minimum_quality_score=72,
                    forward_dates=list(DATES),
                    frozen_prefix_verified=True,
                    warnings=declared["warnings"],
                ),
            )
            write_result(folder / name / label, result)
            before = frozen["metrics"]["ending_equity"]
            after = result["metrics"]["ending_equity"]
            starts = [
                t
                for t in result["trades"] + result["open_positions"]
                if pd.Timestamp(t["entry_time"]) > CUTOFF
            ]
            assert all(
                t["entry_evidence"]["selection"]["quality_score"] >= 72
                for t in starts
                if "entry_evidence" in t
            )
            key = name + ":" + label
            summary[key] = dict(
                metrics=result["metrics"],
                forward_return_pct=(after / before - 1) * 100,
                new_campaigns=len(starts),
                closed_in_forward=len(result["trades"]) - len(frozen["trades"]),
                frozen_prefix_verified=True,
            )
            logger.info(
                "%s continued %.4f%%, total %.4f%%",
                key,
                summary[key]["forward_return_pct"],
                result["metrics"]["total_return_pct"],
            )
    write_json(
        folder / "comparison.json",
        dict(
            results=summary,
            reconstructed_november_signals=data["historical_checks"],
            forward_dates=DATES,
        ),
    )


if __name__ == "__main__":
    main()
