"""Daily warehouse -> native 60F -> atomic trend snapshot refresh.

CLI and lifespan scheduler share the same OS lock and durable progress. Quotes
are upserted over an overlap window; minutes resume from actual per-day gaps,
not a market-wide MAX or a nonempty provider response. Frozen research is untouched.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from core.config import get_config
from core.logger import get_logger
from jobs.sync_intraday import sync_one, SOURCE
from storage.repositories import market as store
from storage.repositories.trend import publish
from tools.kline.calendar import is_trading_day
from tools.market.refresh_state import read_state, write_state, refresh_lock
from tools.market.refresh_clock import expected_date
from tools.market.trend_data import load_dashboard, scan_cutoffs

logger = get_logger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def overlap_start(end, days=5):
    start = end
    for _ in range(days - 1):
        start -= timedelta(days=1)
        while not is_trading_day(start):
            start -= timedelta(days=1)
    return start


def needs_refresh(target):
    """Cheap startup check; only a coverage-verified run may skip the pipeline."""
    state = read_state()
    if state.get("status") != "complete" or state.get("target", "") < target.isoformat():
        return True
    bounds = scan_cutoffs(datetime.now(SHANGHAI).replace(tzinfo=None))
    snapshot = load_dashboard(include_rows=False)
    return not (
        snapshot and str(snapshot["as_of"])[:10] >= target.isoformat()
        and all(value is not None and str(value)[:10] >= target.isoformat()
                for value in bounds.values())
    )


def publish_snapshot(target, coverage, progress=None):
    from jobs.scan_trend import scan
    from skills.trend.strategy import TrendConfig, VERSION
    from skills.trend.mtf import MtfConfig, version_for

    previous = load_dashboard(include_rows=False)
    # Refresh data under the published rules, never silently switch strategy versions.
    settings = previous["metadata"]["config"] if previous else {
        "selection_mode": "mtf", "fresh_reversal": True, "reversal_cycle_reset": True,
    }
    cfg = (MtfConfig if settings.get("selection_mode") == "mtf" else TrendConfig)(**settings)
    cutoff, metadata, rows = scan(target.isoformat(), cfg, workers=2, batch_size=50, progress=progress)
    if cutoff[:10] != target.isoformat():
        raise ValueError("扫描截止日期与任务目标不一致，拒绝发布")
    metadata["refresh_coverage"] = coverage
    run_id = str(uuid4())
    publish(run_id, cutoff, version_for(cfg) if cfg.selection_mode == "mtf" else VERSION, metadata, rows)
    output = Path(get_config().root_dir) / "output" / "market" / "trend"
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{run_id}.json").write_text(json.dumps({
        "run_id": run_id, "as_of": cutoff, "metadata": metadata, "data": rows,
    }, ensure_ascii=False, default=str), encoding="utf-8")
    return {"run_id": run_id, "as_of": cutoff, "universe": len(rows)}


def refresh(target: date, *, start=None, quotes=True, workers=4):
    expected = expected_date()
    if not is_trading_day(target) or expected is None or target > expected:
        raise ValueError("目标必须为已收盘的交易日")
    if not 1 <= workers <= 4:
        raise ValueError("workers必须为1~4")
    start = start or overlap_start(target)
    if start > target:
        raise ValueError("开始日期晚于目标日期")
    with refresh_lock() as acquired:
        if not acquired:
            return {"status": "busy", "target": target.isoformat()}
        previous = read_state()
        state = {"status": "running", "stage": "universe", "target": target.isoformat(),
                 "start": start.isoformat(), "started_at": datetime.now(SHANGHAI).isoformat(),
                 "last_success": previous.get("last_success")}

        def update(**values):
            state.update(values)
            write_state(state)

        update()
        try:
            bounds = scan_cutoffs(datetime.now(SHANGHAI).replace(tzinfo=None))
            oldest = [date.fromisoformat(str(value)[:10]) + timedelta(days=1)
                      for value in bounds.values() if value is not None]
            if oldest:
                start = max(date(2026, 1, 1), min(start, *oldest))
            update(start=start.isoformat())
            from tools.market.universe import fetch_listed_universe
            current = store.listed_universe()
            frame = fetch_listed_universe([r["ts_code"] for r in current if r["board"] == "北交所"])
            store.save_listed_universe(frame, datetime.now(SHANGHAI).date())
            codes = sorted(frame.loc[frame.list_date <= target, "ts_code"].tolist())
            if quotes:
                from tools.market.sync_tushare import TusharePgSyncer
                update(stage="daily")
                update(daily_result=TusharePgSyncer().run(
                    start, target, ["daily", "daily_basic", "adj_factor"], resume=False,
                    allowed_codes=codes))
            coverage = store.daily_coverage(codes, target)
            if coverage["daily_absent"]:
                from tools.market.constituents import full_day_suspensions
                suspended = full_day_suspensions(target)
                coverage["suspended"] = sorted(set(coverage["daily_absent"]) & suspended)
                coverage["daily_unexplained"] = sorted(set(coverage["daily_absent"]) - suspended)
            else:
                coverage.update(suspended=[], daily_unexplained=[])
            summary = {key: len(value) if isinstance(value, list) else value for key, value in coverage.items()}
            update(coverage=summary)
            # A single fresh symbol must never advance an entire market snapshot.
            if not coverage["active"] or coverage["daily_unexplained"] or coverage["factor_absent"]:
                raise ValueError("目标日日线/真实复权因子覆盖不足，保留上一版快照并等待重试")
            audit = store.listed_audit(codes, start, target)
            pending = [r["ts_code"] for r in audit["gaps_against_daily"]]
            update(stage="60F", total=len(pending), completed=0, failures=0)
            failures = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(sync_one, code, start.isoformat(), target.isoformat(), .3): code
                           for code in pending}
                for n, future in enumerate(as_completed(futures), 1):
                    result = future.result()
                    if result["status"] != "ok":
                        failures.append(result)
                    if n % 50 == 0 or n == len(pending):
                        update(completed=n, failures=len(failures))
                        logger.info("盘后60F进度 %s/%s，失败/空 %s", n, len(pending), len(failures))
            update(stage="audit")
            audit = store.listed_audit(codes, start, target)
            store.flag_intraday_gaps(audit["gaps_against_daily"], SOURCE, start, target)
            report = {**audit, "coverage": coverage, "provider_failures": failures}
            root = Path(get_config().root_dir) / "output" / "market" / "refresh"
            (root / f"{target}_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            update(missing_60F=len(audit["gaps_against_daily"]), reference_gaps=len(audit["reference_gaps"]))
            if audit["gaps_against_daily"] or audit["invalid_grid"] or audit["reference_gaps"]:
                raise ValueError("60F缺根/网格或日线因子关联未通过核验，保留上一版快照并等待重试")
            update(stage="snapshot")
            published = publish_snapshot(target, summary,
                                         progress=lambda done, total: update(completed=done, total=total))
            # Whole-day suspensions are explicit exemptions; missing valuation
            # rows remain visible and retryable without blocking valid chart data.
            status = "partial" if coverage["basic_absent"] else "complete"
            update(status=status, stage="done", snapshot=published,
                   last_success=target.isoformat() if status == "complete" else state["last_success"])
        except Exception as error:
            logger.exception("盘后行情更新失败，目标 %s", target)
            update(status="error", error=f"{type(error).__name__}: {error}"[:500])
        return state


def main():
    parser = argparse.ArgumentParser(description="盘后日K/60F与看板更新，可断点重试")
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--skip-quotes", action="store_true", help="仅补分钟与快照，日线必须已入库")
    args = parser.parse_args()
    get_config()
    from jobs.scheduler import DailySyncScheduler
    target = args.end or DailySyncScheduler._expected_latest_trade_date()
    if target is None:
        parser.error("无法确定目标交易日")
    result = refresh(target, quotes=not args.skip_quotes)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
