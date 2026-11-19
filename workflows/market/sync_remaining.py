"""Expand 2026 60F coverage to listed non-ST SH/SZ and existing BJ constituents.

Does not modify index memberships or daily pipeline watermarks. Checkpoints
resume one symbol at a time, including native daily and real factor repairs.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import time
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from workflows.market.sync_intraday import sync_one, SOURCE
from storage.schema.market import ensure_intraday_schema
from storage.repositories import market as store
from storage.repositories.backtest import save_daily
from storage.repositories.trend import save_adjustments
from tools.market.adjustments import fetch_adjustments
from tools.market.universe import fetch_listed_universe

logger = get_logger(__name__)
_DAILY_LOCK = Lock()
_LAST_DAILY = 0.0


def _daily(pro, **params):
    """Share a conservative rate budget across the bounded worker threads."""
    global _LAST_DAILY
    with _DAILY_LOCK:
        time.sleep(max(0, 0.35 - (time.monotonic() - _LAST_DAILY)))
        _LAST_DAILY = time.monotonic()
    return pro.daily(**params)


def repair_reference(row, start, end):
    import tushare as ts

    code = row["ts_code"]
    for attempt in range(3):
        try:
            if row["daily_missing"]:
                pro = ts.pro_api(get_config().tushare_token, timeout=20)
                f = _daily(
                    pro,
                    ts_code=code,
                    start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""),
                )
                if f is None or f.empty or not f.ts_code.eq(code).all():
                    raise ValueError("缺少真实日K或代码不匹配")
                f["trade_date"] = pd.to_datetime(f.trade_date).dt.date
                save_daily(f)
            if row["factor_missing"]:
                save_adjustments(fetch_adjustments(code=code, start=start, end=end))
            return {"ts_code": code, "status": "ok"}
        except Exception as error:
            if attempt == 2:
                return {
                    "ts_code": code,
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}"[:300],
                }
            time.sleep(20 if "频率" in str(error) else attempt + 1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", required=True, help="已完成收盘日 YYYY-MM-DD")
    p.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    p.add_argument("--refresh-universe", action="store_true")
    args = p.parse_args()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if (
        not pd.Timestamp(args.start)
        <= pd.Timestamp(args.end)
        < pd.Timestamp(now.date())
    ):
        p.error("只补已完成的历史交易日；开始日期不得晚于结束日期")
    get_config()
    ensure_intraday_schema()
    members = store.listed_universe()
    if args.refresh_universe or not members:
        existing_bj = [c for c in store.load_universe() if c.endswith(".BJ")]
        f = fetch_listed_universe(existing_bj)
        store.save_listed_universe(f, now.date())
        members = store.listed_universe()
    codes = [
        r["ts_code"] for r in members if r["list_date"] <= pd.Timestamp(args.end).date()
    ]
    done = store.completed_codes(SOURCE, 60, args.start, args.end)
    pending = [c for c in codes if c not in done]
    root = Path(get_config().root_dir) / "output/market/remaining"
    root.mkdir(parents=True, exist_ok=True)
    report = root / (now.strftime("%Y%m%dT%H%M%S") + ".json")
    ledger = report.with_suffix(".jsonl")
    logger.info(
        "非ST补齐: 股票池%s, 跳过完成%s, 待抓%s",
        len(codes),
        len(set(codes) & done),
        len(pending),
    )
    failures = []
    with ledger.open("w", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(sync_one, c, args.start, args.end, 0.4) for c in pending
            ]
            for n, future in enumerate(as_completed(futures), 1):
                r = future.result()
                stream.write(json.dumps(r, ensure_ascii=False) + "\n")
                stream.flush()
                if r["status"] != "ok":
                    failures.append(r)
                if n % 100 == 0 or n == len(pending):
                    logger.info(
                        "剩余60F %s/%s 失败/空%s", n, len(pending), len(failures)
                    )
        gaps = store.reference_gaps(codes, args.start, args.end)
        # Prefer one market-wide date call for shared holes. A date existing
        # somewhere in daily is not evidence that this expanded pool is covered.
        common = Counter(day for r in gaps for day in (r.get("daily_dates") or []))
        if common:
            import tushare as ts

            pro = ts.pro_api(get_config().tushare_token, timeout=20)
            for day, count in sorted(common.items()):
                if count < 20:
                    continue
                try:
                    f = _daily(pro, trade_date=day.strftime("%Y%m%d"), limit=6000)
                    if f is None or f.empty or len(f) >= 6000:
                        raise ValueError("公共日期返回空或可能截断，改用单股补齐")
                    f = f[f.ts_code.isin(codes)].copy()
                    f["trade_date"] = pd.to_datetime(f.trade_date).dt.date
                    if not f.trade_date.eq(day).all():
                        raise ValueError("日K日期不匹配")
                    save_daily(f)
                except Exception as error:
                    logger.warning("公共日K补齐失败 %s: %s", day, type(error).__name__)
            gaps = store.reference_gaps(codes, args.start, args.end)
        logger.info("真实日K/复权因子需补%s只", len(gaps))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(repair_reference, r, args.start, args.end) for r in gaps
            ]
            for n, future in enumerate(as_completed(futures), 1):
                r = future.result()
                stream.write(json.dumps(r, ensure_ascii=False) + "\n")
                stream.flush()
                if r["status"] != "ok":
                    failures.append(r)
                if n % 100 == 0 or n == len(gaps):
                    logger.info("补齐引用 %s/%s", n, len(gaps))
    audit = store.listed_audit(codes, args.start, args.end)
    store.flag_intraday_gaps(audit["gaps_against_daily"], SOURCE, args.start, args.end)
    audit.update(
        universe=len(codes),
        boards=dict(Counter(r["board"] for r in members)),
        attempted=len(pending),
        failures=failures,
        start=args.start,
        end=args.end,
        universe_observed_at=str(now),
        universe_basis="当前上市非ST名单，存在幸存者偏差",
    )
    report.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str(report),
                "universe": len(codes),
                "attempted": len(pending),
                "failures": len(failures),
                **audit["counts"],
            },
            default=str,
        )
    )
    return int(
        bool(
            failures
            or audit["gaps_against_daily"]
            or audit["reference_gaps"]
            or audit["invalid_grid"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
