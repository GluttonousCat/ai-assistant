"""Replay Spring reversal and MA-confirmed LPS with identical volume management."""

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
from jobs.research_quality_60f import (
    BASELINE,
    configuration,
    digest,
    freeze,
    read_report,
)
from jobs.research_wyckoff_60f import PROTECTION
from skills.quant.report import with_benchmarks, write_result
from skills.quant.tranche_engine import simulate
from skills.quant.wyckoff_hybrid import filter_pullbacks

logger = get_logger(__name__)
VERSION = "wyckoff-hybrid-60f-v34"


def main():
    root = Path(get_config().root_dir)
    parent = root / "output/quant/wyckoff_price_volume_v33"
    folder = root / "output/quant/wyckoff_hybrid_v34"
    folder.mkdir(parents=True, exist_ok=True)
    source = parent / "prepared.pkl"
    manifest = parent / "declaration.json"
    baseline_path, baseline = read_report(root / BASELINE)
    prior = json.loads(manifest.read_text(encoding="utf-8"))
    comparison = json.loads((parent / "comparison.json").read_text(encoding="utf-8"))
    if prior["strategy_version"] != "wyckoff-price-volume-60f-v33":
        raise ValueError("量价缓存版本不匹配")
    code = [
        "jobs/research_wyckoff_hybrid_60f.py",
        "skills/quant/wyckoff_hybrid.py",
        "skills/quant/prepare.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/support_protection.py",
        "skills/quant/models.py",
        "skills/quant/tranches.py",
        "skills/quant/metrics.py",
        "skills/quant/report.py",
        "skills/quant/registry.py",
        "jobs/backtest_mtf.py",
        "jobs/research_quality_60f.py",
        "jobs/research_wyckoff_60f.py",
    ]
    for name in (
        "skills/quant/prepare.py",
        "skills/quant/tranche_engine.py",
        "skills/quant/support_protection.py",
    ):
        if digest(root / name) != prior["code_sha256"][name]:
            raise ValueError(f"全量对照撮合代码已改动: {name}")
    declared = freeze(
        folder / "declaration.json",
        dict(
            version=VERSION,
            windows=WINDOWS,
            minimum_quality_score=72,
            inputs={
                str(p.relative_to(root)): digest(p)
                for p in (source, manifest, baseline_path, parent / "comparison.json")
            },
            code_sha256={name: digest(root / name) for name in code},
            entry="Spring-test by price and volume. LPS also requires M13 above M21, rising M21 over four bars, rising M8 over two bars and close at or above M8 at the confirmation bar.",
            selection="Fixed hybrid rule, replayed against v33 and v21 on the same windows and cost scenarios. No parameter selection.",
            warnings=[
                "Repeated 2026 mainboard research with a fixed current universe. Results are historical and subject to membership and survivorship bias.",
                "Reversal entry and all management rules use the same v33 price/volume evidence. Only the LPS entry gate changes.",
                "Completed 60F triggers, after-close plans, next legal open, 532, T+1, actual limits and fees. Risk budget and position caps match the frozen control.",
            ],
        ),
    )
    for name in code:
        target = folder / "source" / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / name).read_bytes())
    with source.open("rb") as stream:
        originals = pickle.load(stream)["price_volume_primary"]
    stocks = []
    for stock in originals:
        if any(s["family"] == "trend_pullback" for s in stock.signals):
            path = root / "output/quant/mainboard_60f_v8/minutes" / f"{stock.code}.pkl"
            if digest(path) != prior["input_sha256"][str(path.relative_to(root))]:
                raise ValueError(f"{stock.code} 60F与量价缓存来源不一致")
            stock = filter_pullbacks(pd.read_pickle(path), stock)
        stocks.append(stock)

    def count(series, family):
        return sum(
            signal["family"] == family for stock in series for signal in stock.signals
        )

    audit = {
        family: {"before": count(originals, family), "after": count(stocks, family)}
        for family in ("base_rebound", "trend_pullback")
    }
    if audit["base_rebound"]["before"] != audit["base_rebound"]["after"]:
        raise AssertionError("底部反转混入均线门槛")
    write_json(folder / "signal_audit.json", audit)
    logger.info("Hybrid signals %s", audit)
    calendar = pd.DatetimeIndex(
        [pd.Timestamp(p["date"]) + pd.Timedelta(hours=15) for p in baseline["equity"]]
    )
    plan, suite = configuration(baseline), str(uuid4())
    metrics = {}
    for window, (start, end) in WINDOWS.items():
        clock = calendar[
            (calendar >= start) & (calendar < pd.Timestamp(end) + pd.Timedelta(days=1))
        ]
        series = window_series(stocks, start, end)
        for stressed in (False, True):
            cfg = replace(
                plan, base=replace(plan.base, slippage=0.001 if stressed else 0.0005)
            )
            result = simulate(
                series, clock, "wyckoff_60f_v31", cfg, wyckoff_policy=PROTECTION
            )
            m = result["metrics"]
            m["worst_trade_return_pct"] = min(
                (t["return_pct"] for t in result["trades"]), default=None
            )
            pnl = sum(t["pnl"] for t in result["trades"]) + sum(
                p["unrealized_pnl"] + p["partial_realized_pnl"]
                for p in result["open_positions"]
            )
            if abs(cfg.base.initial_cash + pnl - m["ending_equity"]) > 1e-5:
                raise AssertionError("组合期末资金对账失败")
            label = window + ("__stress" if stressed else "")
            result.update(
                run_id=str(uuid4()),
                suite_id=suite,
                strategy_id="wyckoff_60f_v31",
                strategy_version=VERSION,
                strategy_config=dict(entry=declared["entry"], wyckoff=prior["config"]),
                timeframe="1D",
                universe_mode="fixed_snapshot",
                start=start,
                end=end,
                diagnostics=dict(
                    research_kind="wyckoff",
                    research_variant=label,
                    in_sample=True,
                    eligible_for_default=False,
                    independent_validation=False,
                    warnings=declared["warnings"],
                    minimum_quality_score=72,
                    quality_scale="wyckoff-native-0-100",
                    universe_size=len(stocks),
                    trend_timeframe="60F",
                    entry_timeframe="1D",
                    execution_schedule="after_close",
                ),
            )
            write_result(folder / label, with_benchmarks(result, baseline["equity"]))
            metrics[label] = m
            logger.info(
                "%s net=%.4f%% drawdown=%.4f%%",
                label,
                m["total_return_pct"],
                m["max_drawdown_pct"],
            )
    write_json(
        folder / "comparison.json", dict(hybrid=metrics, **comparison["results"])
    )


if __name__ == "__main__":
    main()
