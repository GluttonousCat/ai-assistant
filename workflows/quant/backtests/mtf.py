"""Bounded v5 research: 60F context, daily entries, three declared exit policies.

Prepare once, compare policies on Jan-Jun only, then freeze the choice for the
later windows. These windows share a biased fixed membership and are not proof
of unseen performance. The known August example informed the entry design.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4
import json
import pickle
from collections import Counter

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from skills.quant.engine import simulate, EXECUTION_VERSION
from skills.quant.models import BacktestConfig
from skills.quant.prepare import MarketSeries, prepare_mtf, prepare_stock
from skills.quant.report import write_result
from skills.quant.research_report import write_research_report
from skills.quant.registry import STRATEGIES
from skills.trend.mtf import VERSION, version_for, MtfConfig
from storage.pg import retry_on_disconnect as _database_unit
from storage.repositories.backtest import input_dates, publish
from storage.schema.quant import ensure_quant_schema
from tools.market.backtest_data import benchmarks, observed_dates, quotes
from tools.market.trend_data import scan_universe

logger = get_logger(__name__)
START, END = "2026-01-01", "2026-11-12"
BASE = BacktestConfig(risk_budget_pct=0.5, max_entry_risk_pct=5)
PROFILES = {
    "single_line_2r": BASE,
    "structure_2r": replace(BASE, trend_exit=False, max_holding_bars=40),
    "protected_3r": replace(
        BASE, exit_policy="protected", take_profit_r=3, max_holding_bars=60
    ),
}
WINDOWS = {
    "development": (START, "2026-06-30"),
    "validation": ("2026-07-01", "2026-08-31"),
    "time_holdout": ("2026-09-01", END),
    "full": (START, END),
}


def prepare(folder, workers=2, entry_cfg=None):
    entry_cfg = entry_cfg or MtfConfig()
    folder.mkdir(parents=True, exist_ok=True)
    members = scan_universe(END)
    observed = observed_dates([m["ts_code"] for m in members], END)
    for m in members:
        m["observed_at"] = observed[m["ts_code"]]
    declaration = {
        "calculation_version": version_for(entry_cfg),
        "execution_version": EXECUTION_VERSION,
        "suite_id": str(uuid4()),
        "start": START,
        "end": END,
        "strategy_config": asdict(entry_cfg),
        "profiles": {k: asdict(v) for k, v in PROFILES.items()},
        "windows": WINDOWS,
        "selection": "development only: >=30 closed trades, maximize return/max(drawdown,1); no later-window reselection",
        "limitations": [
            "fixed current membership; in-sample entry design",
            "August 24 and November examples were seen before this research",
            "no historical ST or corporate-action cash ledger",
        ],
    }
    (folder / "declaration.json").write_text(
        json.dumps(declaration, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def batch(group):
        codes = [m["ts_code"] for m in group]
        minute = _database_unit(lambda: quotes(codes, START, END, "60F"))
        daily = _database_unit(lambda: quotes(codes, START, END, "1D"))
        mg = dict(tuple(minute.groupby("ts_code"))) if not minute.empty else {}
        dg = dict(tuple(daily.groupby("ts_code"))) if not daily.empty else {}
        stocks, excluded = [], []
        for member in group:
            if "ST" in member["name"].upper() or "退" in member["name"]:
                excluded.append(
                    {"code": member["ts_code"], "reason": "当前简称风险过滤"}
                )
                continue
            stock, issue = prepare_mtf(
                mg.get(member["ts_code"], pd.DataFrame()),
                dg.get(member["ts_code"], pd.DataFrame()),
                member,
                entry_cfg,
            )
            if issue:
                excluded.append({"code": member["ts_code"], "reason": issue})
            else:
                stocks.append(stock)
        return stocks, excluded

    series, excluded = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(batch, members[i : i + 100])
            for i in range(0, len(members), 100)
        ]
        for future in as_completed(futures):
            stocks, skipped = future.result()
            series.extend(stocks)
            excluded.extend(skipped)
            logger.info("双周期准备 %s/%s", len(series) + len(excluded), len(members))
    series.sort(key=lambda s: s.code)
    payload = {
        "declaration": declaration,
        "members": members,
        "series": series,
        "excluded": excluded,
    }
    with (folder / "prepared.pkl").open("wb") as out:
        pickle.dump(payload, out, pickle.HIGHEST_PROTOCOL)
    return payload


def window_series(series, start, end):
    result = []
    for s in series:
        mask = (s.times >= start) & (s.times < pd.Timestamp(end) + pd.Timedelta(days=1))
        indexes = mask.nonzero()[0]
        if not len(indexes):
            continue
        first, last = int(indexes[0]), int(indexes[-1])
        signals = [
            {**sig, "i": sig["i"] - first}
            for sig in s.signals
            if first <= sig["i"] <= last
        ]
        result.append(
            MarketSeries(
                s.code,
                s.name,
                s.observed_at,
                s.times[mask],
                {k: v[mask] for k, v in s.data.items()},
                signals,
            )
        )
    return result


def run(folder, payload):
    series = payload["series"]
    days, limits, _ = input_dates(START, END)
    if set(days) - limits:
        raise ValueError("缺少真实限价日期")
    suite = payload["declaration"]["suite_id"]
    diagnostic = {
        "universe_size": len(payload["members"]),
        "usable_stocks": len(series),
        "bar_count": sum(len(s.times) for s in series),
        "excluded": payload["excluded"],
        "membership_snapshot": str(
            min(m["membership_date"] for m in payload["members"])
        ),
        "first_observed_at": str(min(m["observed_at"] for m in payload["members"])),
        "in_sample": True,
        "trend_timeframe": "60F",
        "entry_timeframe": "1D",
        "warnings": payload["declaration"]["limitations"],
    }
    if not series:
        raise ValueError("无有效双周期样本")
    missing = sum(
        int(
            (
                ~pd.Series(s.data["up_limit"]).gt(0)
                | ~pd.Series(s.data["down_limit"]).gt(0)
            ).sum()
        )
        for s in series
    )
    if missing / diagnostic["bar_count"] > 0.02:
        raise ValueError("真实限价覆盖不足98%")
    base = benchmarks(START, END)
    index = {
        code: {
            str(r["trade_date"]): float(r["close"])
            for r in base
            if r["ts_code"] == code
        }
        for code in ["000852.SH", "000300.SH"]
    }
    comparisons = {}
    selected = None
    full = []
    ensure_quant_schema()

    def complete(result, strategy, start, end, mode, profile, window):
        result.update(
            run_id=str(uuid4()),
            suite_id=suite,
            strategy_id=strategy,
            timeframe="1D",
            universe_mode=mode,
            start=start,
            end=end,
            strategy_version=VERSION,
            strategy_config=asdict(MtfConfig()),
            diagnostics={
                **diagnostic,
                "exit_profile": profile,
                "research_window": window,
                "profile_selected_on": "2026-01-01—2026-06-30",
            },
        )
        bm = {}
        for code, values in index.items():
            first = values.get(result["equity"][0]["date"])
            missing = []
            for point in result["equity"]:
                value = values.get(point["date"])
                point.setdefault("benchmarks", {})[code] = (
                    value / first if value and first else None
                )
                if value is None:
                    missing.append(point["date"])
            last = values.get(result["equity"][-1]["date"])
            bm[code] = {
                "return_pct": (last / first - 1) * 100
                if first and last and not missing
                else None,
                "missing_dates": missing,
            }
        result["benchmark_metrics"] = bm
        result["metrics"]["excess_csi1000_pct"] = (
            result["metrics"]["total_return_pct"] - bm["000852.SH"]["return_pct"]
            if bm["000852.SH"]["return_pct"] is not None
            else None
        )
        return result

    for window, (start, end) in WINDOWS.items():
        stocks = window_series(series, start, end)
        timeline = pd.DatetimeIndex(
            [
                pd.Timestamp(day) + pd.Timedelta(hours=15)
                for day in days
                if start <= str(day) <= end
            ]
        )
        comparisons[window] = {}
        for profile, cfg in PROFILES.items():
            r = complete(
                simulate(stocks, timeline, "mtf_dual_v5", "1D", "fixed_snapshot", cfg),
                "mtf_dual_v5",
                start,
                end,
                "fixed_snapshot",
                profile,
                window,
            )
            comparisons[window][profile] = {
                "metrics": r["metrics"],
                "exits": dict(Counter(t["reason"] for t in r["trades"])),
            }
            sub = folder / window / profile
            write_result(sub, r)
            if window == "full":
                full.append(r)
            logger.info(
                "退出对照 %s %s 收益%.2f 回撤%.2f 胜率%s 平仓%s",
                window,
                profile,
                r["metrics"]["total_return_pct"],
                r["metrics"]["max_drawdown_pct"],
                r["metrics"]["win_rate_pct"],
                r["metrics"]["closed_trades"],
            )
        if window == "development":
            candidates = [
                (k, v["metrics"])
                for k, v in comparisons[window].items()
                if v["metrics"]["closed_trades"] >= 30
            ]
            if not candidates:
                raise ValueError("开发段不足30笔，不能选择退出方案")
            selected = max(
                candidates,
                key=lambda p: p[1]["total_return_pct"]
                / max(p[1]["max_drawdown_pct"], 1),
            )[0]
            (folder / "selection.json").write_text(
                json.dumps(
                    {
                        "profile": selected,
                        "development": comparisons[window],
                        "frozen_before_validation": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    chosen = next(r for r in full if r["diagnostics"]["exit_profile"] == selected)
    others = [r for r in full if r is not chosen]
    # Store controls first; the selected default is the newest dual result.
    for r in others:
        _database_unit(lambda r=r: publish(r))
    timeline = pd.DatetimeIndex(
        [pd.Timestamp(day) + pd.Timedelta(hours=15) for day in days]
    )
    for mode in ["fixed_snapshot", "observed_only"]:
        for strategy, definition in STRATEGIES.items():
            if definition.get("generation") != "v5":
                continue
            if mode == "fixed_snapshot" and strategy == "mtf_dual_v5":
                continue
            r = complete(
                simulate(series, timeline, strategy, "1D", mode, PROFILES[selected]),
                strategy,
                START,
                END,
                mode,
                selected,
                "full",
            )
            _database_unit(lambda r=r: publish(r))
            write_result(folder / "published", r)
    _database_unit(lambda: publish(chosen))
    write_result(folder / "published", chosen)
    report = {
        "suite_id": suite,
        "selected_profile": selected,
        "comparisons": comparisons,
        "diagnostics": diagnostic,
        "default_run_id": chosen["run_id"],
    }
    (folder / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_research_report(folder, report, full)
    print(
        json.dumps(
            {
                "suite_id": suite,
                "selected_profile": selected,
                "default_run_id": chosen["run_id"],
                "metrics": chosen["metrics"],
            },
            ensure_ascii=False,
        )
    )
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepare-only", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument(
        "--legacy-daily",
        action="store_true",
        help="从同一行情缓存重算修复后的旧日K基线",
    )
    p.add_argument("--folder", type=Path, default=Path("output/quant/mtf_v5_research"))
    p.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    a = p.parse_args()
    get_config()
    if a.resume:
        with (a.folder / "prepared.pkl").open("rb") as source:
            payload = pickle.load(source)
        if asdict(MtfConfig(**payload["declaration"]["strategy_config"])) != asdict(
            MtfConfig()
        ):
            raise ValueError("缓存参数不符，请重新准备")
        payload["declaration"].setdefault(
            "calculation_version", "60f-trend-daily-entry-v5.1"
        )
        if payload["declaration"]["calculation_version"] != VERSION:
            raise ValueError("缓存计算版本不符，请重新准备")
        if payload["declaration"].get("execution_version") != EXECUTION_VERSION:
            old_suite = payload["declaration"]["suite_id"]
            for stock in payload["series"]:
                for key in ["open", "high", "low", "close"]:
                    stock.data[key] = (
                        stock.data["adjusted_" + key] / stock.data["scale"]
                    ).copy()
            payload["declaration"].update(
                suite_id=str(uuid4()),
                source_suite=old_suite,
                execution_version=EXECUTION_VERSION,
                cache_recovery="raw OHLC inverted from stored adjusted OHLC and known factor scale; signals unchanged",
            )
            (a.folder / "declaration.json").write_text(
                json.dumps(payload["declaration"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            with (a.folder / "prepared.pkl").open("wb") as target:
                pickle.dump(payload, target, pickle.HIGHEST_PROTOCOL)
    else:
        payload = prepare(a.folder, a.workers)
    if not a.prepare_only:
        if a.legacy_daily:
            legacy_daily(a.folder, payload)
        else:
            run(a.folder, payload)


def legacy_daily(folder, payload):
    """Comparable corrected v4 daily baseline, with the identical native inputs."""
    from skills.trend.strategy import TrendConfig

    stocks = []
    members = {m["ts_code"]: m for m in payload["members"]}
    for source in payload["series"]:
        raw = pd.DataFrame(
            {
                k: source.data[k].copy()
                for k in [
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "up_limit",
                    "down_limit",
                ]
            }
        )
        raw["bar_end"] = source.times
        raw["adj_factor"] = source.data["scale"]
        stock, issue = prepare_stock(raw, members[source.code], "1D")
        if issue:
            raise ValueError(f"基线重算失败{source.code}:{issue}")
        stocks.append(stock)
        if len(stocks) % 200 == 0:
            logger.info("修复日K基线 %s/%s", len(stocks), len(payload["series"]))
    days, _, _ = input_dates(START, END)
    timeline = pd.DatetimeIndex(
        [pd.Timestamp(day) + pd.Timedelta(hours=15) for day in days]
    )
    results = []
    suite = str(uuid4())
    for strategy, definition in STRATEGIES.items():
        if definition.get("generation") in ("v5", "rules"):
            continue
        result = simulate(stocks, timeline, strategy, "1D")
        result.update(
            run_id=str(uuid4()),
            suite_id=suite,
            strategy_id=strategy,
            timeframe="1D",
            universe_mode="fixed_snapshot",
            start=START,
            end=END,
            strategy_version="dual-setups-v4.1",
            strategy_config=asdict(TrendConfig()),
            diagnostics={
                "universe_size": len(payload["members"]),
                "usable_stocks": len(stocks),
                "bar_count": sum(len(s.times) for s in stocks),
                "first_observed_at": str(min(s.observed_at for s in stocks)),
                "membership_snapshot": str(
                    min(m["membership_date"] for m in payload["members"])
                ),
                "in_sample": True,
                "execution_repaired": True,
                "warnings": [
                    "固定名单成分偏差；样本内研究",
                    "原始价/复权价隔离后的旧日K基线，不能使用此前失真数值比较",
                ],
            },
        )
        write_result(folder / "corrected_daily_baseline", result)
        _database_unit(lambda result=result: publish(result))
        results.append(result)
        logger.info(
            "修复日K基线结果 %s 收益%s 胜率%s",
            strategy,
            result["metrics"]["total_return_pct"],
            result["metrics"]["win_rate_pct"],
        )
    (folder / "corrected_daily_baseline.json").write_text(
        json.dumps(
            {
                "suite_id": suite,
                "runs": [
                    {
                        "strategy_id": r["strategy_id"],
                        "run_id": r["run_id"],
                        "metrics": r["metrics"],
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
