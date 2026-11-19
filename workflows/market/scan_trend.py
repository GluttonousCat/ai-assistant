"""Offline 60F screener. HTTP serves snapshots and never scans the whole pool.

Run: python -m workflows.market.scan_trend --as-of 2026-11-11
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
from storage.pg import retry_on_disconnect
from tools.market.universe import board_of, risk_name
from skills.trend.strategy import VERSION, TrendConfig, screen
from skills.trend.parent_context import annotate_summary, VERSION as CONTEXT_VERSION
from skills.trend.mtf import (
    MtfConfig,
    evaluate as evaluate_mtf,
    result as mtf_result,
    version_for,
)
from storage.schema.trend import ensure_trend_schema
from storage.repositories.trend import publish
from tools.market.trend_data import (
    scan_cutoffs,
    scan_inputs,
    scan_daily_inputs,
    scan_universe,
    adjust_prices,
    load_dashboard,
)

logger = get_logger(__name__)


def _scan_batch(batch, start, cutoff, cfg, daily_cutoff=None):
    rows = []
    inputs = retry_on_disconnect(
        lambda: scan_inputs([m["ts_code"] for m in batch], start, cutoff)
    )
    groups = dict(tuple(inputs.groupby("ts_code"))) if not inputs.empty else {}
    daily = (
        retry_on_disconnect(
            lambda: scan_daily_inputs(
                [m["ts_code"] for m in batch], start, daily_cutoff
            )
        )
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
        if cfg.selection_mode == "mtf":
            minute, minute_issue = adjust_prices(
                groups.get(m["ts_code"], pd.DataFrame())
            )
            native, daily_issue = adjust_prices(
                daily_groups.get(m["ts_code"], pd.DataFrame())
            )
            issue = minute_issue or daily_issue
            if not issue and (
                pd.Timestamp(minute.bar_end.max()) != pd.Timestamp(cutoff)
                or pd.Timestamp(native.bar_end.max()) != pd.Timestamp(daily_cutoff)
            ):
                issue = "末根K线滞后（停牌或数据未更新）"
            if issue:
                result = {
                    "state": "数据待补",
                    "eligible": False,
                    "score": 0,
                    "reason": issue,
                    "checks": [],
                    "timeframe": "1D",
                    "trend60": {"ready": False, "state": "数据待补"},
                }
            else:
                _, evaluated = evaluate_mtf(minute, native, cfg)
                result = mtf_result(evaluated, cfg)
        else:
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
        if daily_cutoff:
            context_end = min(pd.Timestamp(cutoff), pd.Timestamp(daily_cutoff))
            context_input = daily_groups.get(m["ts_code"], pd.DataFrame())
            if not context_input.empty:
                context_input = context_input[
                    pd.to_datetime(context_input.bar_end) <= context_end
                ]
            context_daily, context_issue = adjust_prices(context_input)
            result = annotate_summary(
                result,
                context_daily if not context_issue else pd.DataFrame(),
                context_end,
            )
        rows.append(
            {
                **m,
                "board": m.get("board") or board_of(m["ts_code"]),
                "membership_date": str(m["membership_date"]),
                **(
                    {"observed_at": str(m["observed_at"])}
                    if m.get("observed_at")
                    else {}
                ),
                **result,
            }
        )
    return rows


def scan(
    as_of: str | None = None,
    cfg: TrendConfig = TrendConfig(),
    workers: int = 2,
    batch_size: int = 100,
    progress=None,
):
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("batch_size必须为1到100")
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    available = None
    if as_of and getattr(cfg, "risk_setups", False):
        previous = retry_on_disconnect(lambda: load_dashboard(None, False))
        if previous and previous["metadata"].get("factor_latest"):
            meta = previous["metadata"]
            bounds = dict(
                bar_end=pd.Timestamp(meta["data_latest"]).to_pydatetime(),
                factor_date=pd.Timestamp(meta["factor_latest"]).date(),
                daily_end=pd.Timestamp(
                    meta.get("timeframes", {})
                    .get("1D", {})
                    .get("as_of", previous["as_of"])
                ).date(),
                snapshot_run_id=previous["run_id"],
            )
            requested = pd.Timestamp(as_of).date()
            if requested <= min(
                bounds["bar_end"].date(), bounds["factor_date"], bounds["daily_end"]
            ):
                available = bounds
    if available is None:
        available = retry_on_disconnect(lambda: scan_cutoffs(now))
    logger.info("趋势扫描截止已核实，加载股票池")
    if not available["bar_end"] or not available["factor_date"]:
        raise ValueError("缺少60F行情或复权因子")
    latest = min(
        available["bar_end"], datetime.combine(available["factor_date"], time(15))
    )
    if cfg.selection_mode == "mtf":
        if not available.get("daily_end"):
            raise ValueError("双周期策略缺少原生日K")
        minute_day = available["bar_end"].date()
        if available["bar_end"].time() < time(15):
            from datetime import timedelta

            minute_day -= timedelta(days=1)
        latest = min(
            latest,
            datetime.combine(minute_day, time(15)),
            datetime.combine(available["daily_end"], time(15)),
        )
    cutoff = (
        datetime.combine(datetime.fromisoformat(as_of).date(), time(15))
        if as_of
        else latest
    )
    if cutoff > latest:
        raise ValueError(f"可验证的策略截止时间为 {latest}; 不推定缺失的复权因子")
    fresh = cfg.selection_mode == "mtf" and cfg.fresh_reversal
    members = (
        scan_universe(str(cutoff.date()), listed=True)
        if fresh
        else scan_universe(str(cutoff.date()))
    )
    if fresh:
        members = [m for m in members if not risk_name(m["name"])]
    if not members:
        raise ValueError("该日期之前没有成分股快照，不能用未来成分冒充历史股票池")
    logger.info("扫描股票池加载完成: %s只", len(members))
    start = "2026-01-01"
    daily_cutoff = None
    if cfg.selection_mode in ("dual", "mtf") and available.get("daily_end"):
        daily_latest = min(
            datetime.combine(available["daily_end"], time(15)),
            datetime.combine(available["factor_date"], time(15)),
        )
        daily_cutoff = (
            datetime.combine(datetime.fromisoformat(as_of).date(), time(15))
            if as_of
            else cutoff
            if cfg.selection_mode == "mtf"
            else daily_latest
        )
        if daily_cutoff > daily_latest:
            raise ValueError(f"日K可验证截止为{daily_latest}")
    if not 1 <= workers <= 4:
        raise ValueError("workers必须在1到4之间")
    rows = []
    if progress:
        progress(0, len(members))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [
            pool.submit(
                _scan_batch,
                members[i : i + batch_size],
                start,
                cutoff.isoformat(),
                cfg,
                daily_cutoff.isoformat() if daily_cutoff else None,
            )
            for i in range(0, len(members), batch_size)
        ]
        for future in as_completed(futures):
            rows.extend(future.result())
            if progress:
                progress(len(rows), len(members))
            logger.info("60F筛选 %s/%s", len(rows), len(members))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    rows.sort(key=lambda r: r["ts_code"])
    metadata = {
        "parent_context_version": CONTEXT_VERSION,
        "opportunity_patterns": dict(
            Counter(
                r.get("context_pattern") or r.get("pattern", "")
                for r in rows
                if r["eligible"]
            )
        ),
        "config": asdict(cfg),
        "cutoffs_source": available.get("snapshot_run_id", "local_latest"),
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
    if fresh:
        metadata.update(
            universe_basis="当前非ST上市名单（沪深三板及原有北交所）；非历史逐日成员，存在幸存者偏差",
            boards=dict(Counter(r["board"] for r in rows)),
            reversal_watch=sum(r.get("trend60", {}).get("fresh", False) for r in rows),
            opportunity_watch=sum(r.get("setup_watch", False) for r in rows),
        )
    if all(r["state"] == "数据待补" for r in period_results):
        raise ValueError(
            "全池数据检查未通过；请先运行 python -m workflows.market.repair_factors，未发布本轮快照"
        )
    if cfg.selection_mode in ("dual", "mtf"):

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

        if cfg.selection_mode == "mtf":
            trend_rows = [
                {
                    "state": r.get("trend60", {}).get("state", "数据待补"),
                    "eligible": False,
                }
                for r in rows
            ]
            metadata.update(
                entry_timeframe="1D",
                trend_timeframe="60F",
                trend_ready=sum(r.get("trend60", {}).get("ready", False) for r in rows),
                method="60F M8/M13/M21确认趋势，原生日K转强确认入场；下一交易日开盘模型；参数仍需后续新样本验证",
            )
            if fresh:
                metadata["method"] = (
                    "60F阴跌/深坑后的低位早期反转观察；起点不因局部回调重置，限12根/前4推进阳线；日K确认后次日开盘模型，低位不保证安全或收益"
                )
            if getattr(cfg, "risk_setups", False):
                metadata["method"] = (
                    "按安全边际区分低位反转、缺口守稳与趋势回调（首次/后续），而非按已涨幅度一刀切；日K收盘确认、次日开盘模型；仍未证明盈利优势"
                )
            metadata["timeframes"] = {
                "1D": stats(rows, daily_cutoff, daily_cutoff),
                "60F": stats(trend_rows, cutoff, available["bar_end"]),
            }
        else:
            metadata["timeframes"] = {
                "60F": stats(rows, cutoff, available["bar_end"]),
                "1D": stats([r["daily"] for r in rows], daily_cutoff, daily_cutoff),
            }
    return max(cutoff, daily_cutoff or cutoff).isoformat(), metadata, rows


def main():
    parser = argparse.ArgumentParser(description="60F主趋势离线筛票")
    parser.add_argument("--as-of", help="完整收盘日，默认行情与复权因子共有的最新日期")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="每个可重试读取单元的股票数，1~100；弱网络可用10",
    )
    parser.add_argument(
        "--config", type=Path, help="TrendConfig/MtfConfig冻结参数的JSON文件"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        choices=range(1, 5),
        help="有界批次并发，默认2",
    )
    args = parser.parse_args()
    get_config()
    settings = (
        json.loads(args.config.read_text(encoding="utf-8"))
        if args.config
        else {
            "selection_mode": "mtf",
            "fresh_reversal": True,
            "reversal_cycle_reset": True,
            "risk_setups": True,
        }
    )
    cfg = (MtfConfig if settings.get("selection_mode") == "mtf" else TrendConfig)(
        **settings
    )
    cutoff, metadata, rows = scan(args.as_of, cfg, args.workers, args.batch_size)
    ensure_trend_schema()
    run_id = str(uuid4())
    publish(
        run_id,
        cutoff,
        version_for(cfg) if cfg.selection_mode == "mtf" else VERSION,
        metadata,
        rows,
    )
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
