"""Offline 60F screener. HTTP serves snapshots and never scans the whole pool.

Run: python -m jobs.scan_trend --as-of 2026-11-11
"""

import argparse
from collections import Counter
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
    scan_universe,
    adjust_prices,
)

logger = get_logger(__name__)


def scan(as_of: str | None = None, cfg: TrendConfig = TrendConfig()):
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
    rows = []
    for offset in range(0, len(members), 100):
        batch = members[offset : offset + 100]
        inputs = scan_inputs([m["ts_code"] for m in batch], start, cutoff.isoformat())
        groups = dict(tuple(inputs.groupby("ts_code"))) if not inputs.empty else {}
        for m in batch:
            frame, issue = adjust_prices(groups.get(m["ts_code"], pd.DataFrame()))
            if not issue and pd.Timestamp(frame.bar_end.max()) != pd.Timestamp(cutoff):
                issue = "末根K线滞后（停牌或数据未更新）"
            if issue:
                result = {
                    "state": "数据待补",
                    "eligible": False,
                    "score": 0,
                    "reason": issue,
                    "checks": [],
                }
            else:
                result = screen(frame, cfg)
            result.pop("config", None)
            if "ST" in m["name"].upper() or "退" in m["name"]:
                result.update(
                    eligible=False,
                    state="风险标的过滤",
                    reason="当前简称含ST或退市标记",
                )
            rows.append({**m, "membership_date": str(m["membership_date"]), **result})
        logger.info("60F筛选 %s/%s", min(offset + 100, len(members)), len(members))
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
        "method": "视频理念的量化代理；阈值是本项目v1假设，尚未验证超额收益",
    }
    if all(r["state"] == "数据待补" for r in rows):
        raise ValueError(
            "全池数据检查未通过；请先运行 python -m jobs.repair_trend_factors，未发布本轮快照"
        )
    return cutoff.isoformat(), metadata, rows


def main():
    parser = argparse.ArgumentParser(description="60F主趋势离线筛票")
    parser.add_argument("--as-of", help="完整收盘日，默认行情与复权因子共有的最新日期")
    parser.add_argument("--config", type=Path, help="TrendConfig字段的JSON文件")
    args = parser.parse_args()
    get_config()
    cfg = (
        TrendConfig(**json.loads(args.config.read_text(encoding="utf-8")))
        if args.config
        else TrendConfig()
    )
    cutoff, metadata, rows = scan(args.as_of, cfg)
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
