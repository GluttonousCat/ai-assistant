"""Offline strategy suite. HTTP only reads published results; never runs a scan."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
import json
import time as time  # Legacy callers patch time.sleep for retry tests.
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from core.config import get_config
from core.logger import get_logger
from skills.trend.strategy import TrendConfig, VERSION
from skills.quant.registry import STRATEGIES
from skills.quant.prepare import prepare_stock
from skills.quant.engine import simulate
from skills.quant.report import write_result, write_summary
from tools.market.trend_data import scan_universe, scan_cutoffs
from tools.market.backtest_data import quotes, benchmarks, observed_dates
from storage.repositories.backtest import publish, input_dates
from storage.pg import retry_on_disconnect as _database_unit
from storage.schema.quant import ensure_quant_schema

logger = get_logger(__name__)


def _prepare_batch(members, start, end, timeframe):
    raw = _database_unit(
        lambda: quotes([m["ts_code"] for m in members], start, end, timeframe)
    )
    groups = dict(tuple(raw.groupby("ts_code"))) if not raw.empty else {}
    series = []
    excluded = []
    for m in members:
        if "ST" in m["name"].upper() or "退" in m["name"]:
            excluded.append(
                {"code": m["ts_code"], "reason": "当前简称风险过滤（无法恢复历史状态）"}
            )
            continue
        stock, issue = prepare_stock(
            groups.get(m["ts_code"], pd.DataFrame()), m, timeframe
        )
        if issue:
            excluded.append({"code": m["ts_code"], "reason": issue})
        else:
            series.append(stock)
    return series, excluded


def run(start, end, periods, workers=2, max_stocks=None):
    get_config()
    ensure_quant_schema()
    cutoffs = scan_cutoffs(datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None))
    latest = min(
        pd.Timestamp(cutoffs["bar_end"]).date(),
        cutoffs["daily_end"],
        cutoffs["factor_date"],
    )
    if pd.Timestamp(end).date() > latest:
        raise ValueError(f"最新可验证数据只有{latest}，不能回测未来或未完成数据")
    members = scan_universe(end)
    if max_stocks:
        members = members[:max_stocks]
    observed = observed_dates([m["ts_code"] for m in members], end)
    for m in members:
        m["observed_at"] = observed[m["ts_code"]]
    days, limit_days, _ = input_dates(start, end)
    if set(days) - limit_days:
        raise ValueError(
            "缺少真实涨跌停日期；先运行 python -m jobs.sync_backtest_inputs"
        )
    base = benchmarks(start, end)
    indexed = {
        code: {
            str(r["trade_date"]): float(r["close"])
            for r in base
            if r["ts_code"] == code
        }
        for code in ["000300.SH", "000852.SH"]
    }
    suite = str(uuid4())
    folder = Path(get_config().root_dir) / "output" / "quant" / suite
    results = []
    for timeframe in periods:
        series = []
        excluded = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_prepare_batch, members[i : i + 100], start, end, timeframe)
                for i in range(0, len(members), 100)
            ]
            done = 0
            for future in as_completed(futures):
                s, e = future.result()
                series.extend(s)
                excluded.extend(e)
                done += len(s) + len(e)
                logger.info("回测准备 %s %s/%s", timeframe, done, len(members))
        series.sort(key=lambda s: s.code)
        total = sum(len(s.times) for s in series)
        missing = sum(
            int(
                (
                    ~np.isfinite(s.data["up_limit"])
                    | ~np.isfinite(s.data["down_limit"])
                ).sum()
            )
            for s in series
        )
        if not total or missing / total > 0.02:
            raise ValueError(
                f"{timeframe}真实涨跌停覆盖不足98%，拒绝将空交易解释为策略表现"
            )
        timeline = pd.DatetimeIndex(
            [
                pd.Timestamp(day) + pd.Timedelta(hours=h, minutes=m)
                for day in days
                for h, m in (
                    [(10, 30), (11, 30), (14, 0), (15, 0)]
                    if timeframe == "60F"
                    else [(15, 0)]
                )
            ]
        )
        diagnostics = {
            "universe_size": len(members),
            "usable_stocks": len(series),
            "excluded": excluded,
            "excluded_reasons": dict(Counter(e["reason"] for e in excluded)),
            "bar_count": total,
            "missing_limit_bars": missing,
            "membership_snapshot": str(min(m["membership_date"] for m in members)),
            "first_observed_at": str(min(observed.values())),
            "in_sample": True,
            "warnings": [
                "固定名单全年回放存在成分前视与幸存者偏差",
                "策略参考过2026样例，本次为样本内检验",
                "仅2026数据，年初存在指标预热期",
                "复权份额近似现金分红/送配，非逐笔权益台账",
                "限价队列、逐笔冲击和历史ST状态不完整；非真实交易收益",
            ],
        }
        for mode in ["fixed_snapshot", "observed_only"]:
            for strategy_id in STRATEGIES:
                if STRATEGIES[strategy_id].get("research_only"):
                    continue
                if STRATEGIES[strategy_id].get("generation") in (
                    "v5",
                    "v6",
                    "v7",
                    "v8",
                    "v9",
                    "rules",
                ):
                    continue  # v5 needs both timeframes; use jobs.backtest_mtf.
                result = simulate(series, timeline, strategy_id, timeframe, mode)
                result.update(
                    run_id=str(uuid4()),
                    suite_id=suite,
                    strategy_id=strategy_id,
                    timeframe=timeframe,
                    universe_mode=mode,
                    start=start,
                    end=end,
                    strategy_version=VERSION,
                    strategy_config=asdict(TrendConfig()),
                    diagnostics=diagnostics,
                )
                benchmark_metrics = {}
                for code, values in indexed.items():
                    first = values.get(result["equity"][0]["date"])
                    last = values.get(result["equity"][-1]["date"])
                    missing_dates = []
                    for point in result["equity"]:
                        price = values.get(point["date"])
                        point.setdefault("benchmarks", {})[code] = (
                            price / first if first and price else None
                        )
                        if price is None:
                            missing_dates.append(point["date"])
                    benchmark_metrics[code] = {
                        "return_pct": (last / first - 1) * 100
                        if first and last and not missing_dates
                        else None,
                        "missing_dates": missing_dates,
                    }
                result["benchmark_metrics"] = benchmark_metrics
                result["metrics"]["excess_csi1000_pct"] = (
                    result["metrics"]["total_return_pct"]
                    - benchmark_metrics["000852.SH"]["return_pct"]
                    if benchmark_metrics["000852.SH"]["return_pct"] is not None
                    else None
                )
                write_result(folder, result)
                _database_unit(lambda: publish(result))
                results.append(result)
                logger.info(
                    "回测完成 %s %s %s 收益%.2f%% 回撤%.2f%% 平仓%s",
                    timeframe,
                    strategy_id,
                    mode,
                    result["metrics"]["total_return_pct"],
                    result["metrics"]["max_drawdown_pct"],
                    result["metrics"]["closed_trades"],
                )
        del series
    write_summary(folder, results)
    print(
        json.dumps(
            {"suite_id": suite, "output": str(folder), "runs": len(results)},
            ensure_ascii=False,
        )
    )
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-11-12")
    p.add_argument("--periods", nargs="+", choices=["60F", "1D"], default=["60F", "1D"])
    p.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    p.add_argument(
        "--max-stocks", type=int, help="仅用于开发冒烟，输出会记录实际样本数"
    )
    a = p.parse_args()
    if a.start > a.end or a.max_stocks is not None and a.max_stocks < 1:
        p.error("日期或样本上限不合法")
    run(a.start, a.end, a.periods, a.workers, a.max_stocks)


if __name__ == "__main__":
    main()
