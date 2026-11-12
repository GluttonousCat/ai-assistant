"""Resumable 2026 60-minute import for the five-index constituent universe.

Providers own network calls; repositories own SQL; this job owns orchestration.
One symbol commits atomically with its checkpoint. No DDL or network wait occurs
inside a business transaction. Failed symbols remain retryable on the next run.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from core.config import get_config
from core.logger import get_logger
from storage.schema.market import ensure_intraday_schema
from storage.repositories import market as repository
from tools.market.constituents import fetch_constituents, fetch_suspensions
from tools.market.sina_transport import fetch_sina_history

logger = get_logger(__name__)
SOURCE = "akshare-sina"


def sync_one(code: str, start: str, end: str, pace: float) -> dict:
    frame = None
    last_error = None
    for attempt in range(3):
        try:
            if frame is None:
                frame = fetch_sina_history(code, start, end)
            result = repository.save_result(code, 60, SOURCE, start, end, frame)
            time.sleep(pace)
            return result
        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"[:500]
            time.sleep(1 + attempt * 2)
    try:
        repository.save_result(code, 60, SOURCE, start, end, None, last_error)
    except Exception as error:
        logger.error("保存失败状态失败 %s: %s", code, type(error).__name__)
    return {"ts_code": code, "status": "error", "error": last_error, "rows": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description="指数成分股2026年60分钟K线入库")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pace", type=float, default=.3)
    parser.add_argument("--refresh-universe", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="仅供首次小批量验证；0表示整个股票池")
    parser.add_argument("--retry-all", action="store_true", help="重新同步已完成项，验证幂等或回补修订")
    args = parser.parse_args()
    if not 1 <= args.workers <= 6 or args.pace < 0:
        parser.error("workers 必须为1~6，pace不得为负")
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if start > end:
        parser.error("开始日期晚于结束日期")
    get_config()
    ensure_intraday_schema()
    universe = repository.load_universe()
    if args.refresh_universe or not universe:
        frame = fetch_constituents(end)
        repository.save_memberships(frame)
        universe = repository.load_universe()
    done = set() if args.retry_all else repository.completed_codes(SOURCE, 60, args.start, args.end)
    codes = [code for code in universe if code not in done]
    if args.limit:
        codes = codes[:args.limit]
    root = Path(get_config().root_dir) / "output" / "market" / "intraday_import"
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S")
    logger.info("60F导入: 股票池%s, 已完成%s, 本轮%s, %s~%s", len(universe), len(done), len(codes), args.start, args.end)
    failures = []
    imported = 0
    with (root / f"{run_id}.jsonl").open("w", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(sync_one, code, args.start, args.end, args.pace): code for code in codes}
            for n, future in enumerate(as_completed(futures), 1):
                result = future.result()
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                stream.flush()
                imported += result["rows"]
                if result["status"] != "ok":
                    failures.append(result)
                if n % 100 == 0 or n == len(codes):
                    logger.info("60F进度 %s/%s, 行数%s, 失败/空%s", n, len(codes), imported, len(failures))
    audit = repository.audit(SOURCE, 60, args.start, args.end)
    if audit["latest_day_absent"]:
        try:
            audit["suspensions_on_last_day"] = fetch_suspensions(
                [r["ts_code"] for r in audit["latest_day_absent"]], audit["last_trade_date"])
        except Exception as error:
            audit["suspension_check_error"] = type(error).__name__
    audit.update(universe=len(universe), attempted=len(codes), imported=imported, failures=failures)
    report = root / f"{run_id}_audit.json"
    report.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report": str(report), **audit["counts"], "failures": len(failures)}, default=str))
    return 1 if failures or audit["gaps_against_daily"] or audit["invalid_60m_grid_samples"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
