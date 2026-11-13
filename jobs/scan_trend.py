"""Offline 60F screener. HTTP serves snapshots and never scans the whole pool.

Run: python -m jobs.scan_trend --as-of 2026-11-11
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, time
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
from core.config import get_config
from core.logger import get_logger
from skills.trend.strategy import VERSION, TrendConfig, screen
from storage.schema.trend import ensure_trend_schema
from storage.repositories.trend import publish
from tools.market.trend_data import (
    scan_cutoffs,
    scan_inputs,
    scan_daily_inputs,
    scan_universe,
    adjust_prices,
)

logger = get_logger(__name__)


def _scan_batch(batch, start, cutoff, cfg, daily_cutoff=None):
    rows = []
    inputs = scan_inputs([m["ts_code"] for m in batch], start, cutoff)
    groups = dict(tuple(inputs.groupby("ts_code"))) if not inputs.empty else {}
    daily = (
        scan_daily_inputs([m["ts_code"] for m in batch], start, daily_cutoff)
        if daily_cutoff
        else pd.DataFrame()
    )
    daily_groups = dict(tuple(daily.groupby("ts_code"))) if not daily.empty else {}

    def evaluate(frame, expected, period):
        frame, issue = adjust_prices(frame)
        if not issue and pd.Timestamp(frame.bar_end.max()) != pd.Timestamp(expected):
            issue = "末根K线滞后（停牌或数据未更新）"
        if issue:
            return {
                "state": "数据待补",
                "eligible": False,
                "score": 0,
                "reason": issue if not frame.empty else f"没有{period}原生行情",
                "checks": [],
                "timeframe": period,
            }
        result = screen(frame, cfg, period)
        result.pop("config", None)
        return result

    for m in batch:
        result = evaluate(groups.get(m["ts_code"], pd.DataFrame()), cutoff, "60F")
        if cfg.selection_mode == "dual":
            result["daily"] = evaluate(
                daily_groups.get(m["ts_code"], pd.DataFrame()), daily_cutoff, "1D"
            )
        if "ST" in m["name"].upper() or "退" in m["name"]:
            result.update(
                eligible=False,
                state="风险标的过滤",
                reason="当前简称含ST或退市标记",
            )
            if "daily" in result:
                result["daily"].update(
                    eligible=False,
                    state="风险标的过滤",
                    reason="当前简称含ST或退市标记",
                )
        rows.append({**m, "membership_date": str(m["membership_date"]), **result})
    return rows


def scan(as_of: str | None = None, cfg: TrendConfig = TrendConfig(), workers: int = 2):
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    available = scan_cutoffs(now)
    if not available["bar_end"] or not available["factor_date"]:
        raise ValueError("缺少60F行情或复权因子")
    latest = min(
        available["bar_end"], datetime.combine(available["factor_date"], time(15))
    )
    cutoff = (
        datetime.combine(datetime.fromisoformat(as_of).date(), time(15))
        if as_of
        else latest
    )
    if cutoff > latest:
        raise ValueError(f"可验证的策略截止时间为 {latest}; 不推定缺失的复权因子")
    members = scan_universe(str(cutoff.date()))
    if not members:
        raise ValueError("该日期之前没有成分股快照，不能用未来成分冒充历史股票池")
    start = "2026-01-01"
    daily_cutoff = None
    if cfg.selection_mode == "dual" and available.get("daily_end"):
        daily_latest = min(
            datetime.combine(available["daily_end"], time(15)),
            datetime.combine(available["factor_date"], time(15)),
        )
        daily_cutoff = (
            datetime.combine(datetime.fromisoformat(as_of).date(), time(15))
            if as_of
            else daily_latest
        )
        if daily_cutoff > daily_latest:
            raise ValueError(f"日K可验证截止为{daily_latest}")
    if not 1 <= workers <= 4:
        raise ValueError("workers必须在1到4之间")
    rows = []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [
            pool.submit(
                _scan_batch,
                members[i : i + 100],
                start,
                cutoff.isoformat(),
                cfg,
                daily_cutoff.isoformat() if daily_cutoff else None,
            )
            for i in range(0, len(members), 100)
        ]
        for future in as_completed(futures):
            rows.extend(future.result())
            logger.info("60F筛选 %s/%s", len(rows), len(members))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    rows.sort(key=lambda r: r["ts_code"])
    metadata = {
        "config": asdict(cfg),
        "universe": len(members),
        "eligible": sum(r["eligible"] for r in rows),
        "states": dict(Counter(r["state"] for r in rows)),
        "data_latest": str(available["bar_end"]),
        "factor_latest": str(available["factor_date"]),
        "start": start,
        "price_basis": "forward_asof",
        "universe_basis": "当时可用的最近成分快照；不是逐日历史成员回放",
        "method": "两类机会：趋势回调 / 阴跌与砸坑反弹；60F与原生日K分开筛选；保留前次起势背景，回调再起不冒充首阳；尚未验证超额收益",
    }
    period_results = rows + [r["daily"] for r in rows if "daily" in r]
    if all(r["state"] == "数据待补" for r in period_results):
        raise ValueError(
            "全池数据检查未通过；请先运行 python -m jobs.repair_trend_factors，未发布本轮快照"
        )
    if cfg.selection_mode == "dual":

        def stats(records, end, latest):
            return {
                "as_of": str(end or cutoff),
                "data_latest": str(latest or end or cutoff),
                "universe": len(records),
                "eligible": sum(r["eligible"] for r in records),
                "states": dict(Counter(r["state"] for r in records)),
                "patterns": dict(
                    Counter(
                        r.get("pattern_label", "") for r in records if r["eligible"]
                    )
                ),
            }

        metadata["timeframes"] = {
            "60F": stats(rows, cutoff, available["bar_end"]),
            "1D": stats([r["daily"] for r in rows], daily_cutoff, daily_cutoff),
        }
    return max(cutoff, daily_cutoff or cutoff).isoformat(), metadata, rows


def main():
    parser = argparse.ArgumentParser(description="60F主趋势离线筛票")
    parser.add_argument("--as-of", help="完整收盘日，默认行情与复权因子共有的最新日期")
    parser.add_argument("--config", type=Path, help="TrendConfig字段的JSON文件")
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        choices=range(1, 5),
        help="有界批次并发，默认2",
    )
    args = parser.parse_args()
    get_config()
    cfg = (
        TrendConfig(**json.loads(args.config.read_text(encoding="utf-8")))
        if args.config
        else TrendConfig()
    )
    cutoff, metadata, rows = scan(args.as_of, cfg, args.workers)
    ensure_trend_schema()
    run_id = str(uuid4())
    publish(run_id, cutoff, VERSION, metadata, rows)
    output = Path(get_config().root_dir) / "output" / "market" / "trend"
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{run_id}.json").write_text(
        json.dumps(
            {"run_id": run_id, "as_of": cutoff, "metadata": metadata, "data": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "run_id": run_id,
                "as_of": cutoff,
                "universe": metadata["universe"],
                "states": metadata["states"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
