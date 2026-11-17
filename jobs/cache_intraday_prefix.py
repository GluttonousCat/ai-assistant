"""Collect a fixed, completed intraday prefix, separate from end-of-day state."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import pandas as pd

from core.config import get_config
from core.local_state import file_lock, read_json, write_json
from core.logger import get_logger
from storage.repositories.market import save_closed_prefix
from storage.repositories.trend import save_adjustments
from tools.market.adjustments import fetch_adjustments
from tools.market.sina_transport import fetch_sina_history
from tools.market.trend_data import scan_universe, load_dashboard
from tools.market.universe import risk_name

logger = get_logger(__name__)
SLOTS = ("10:30", "11:30", "14:00", "15:00")


def closed_prefix(frame, day, cutoff, now):
    boundary = pd.Timestamp(f"{day} {cutoff}:00")
    clock = pd.Timestamp(now)
    if clock.tzinfo:
        clock = clock.tz_convert("Asia/Shanghai").tz_localize(None)
    if boundary > clock:
        raise ValueError("请求的60F尚未收盘")
    expected = [f"{day} {slot}:00" for slot in SLOTS if slot <= cutoff]
    filtered = frame[(pd.to_datetime(frame.bar_end).dt.date == boundary.date()) & (pd.to_datetime(frame.bar_end) <= boundary)].copy()
    actual = list(pd.to_datetime(filtered.bar_end).dt.strftime("%Y-%m-%d %H:%M:%S"))
    if actual != expected:
        raise ValueError("今天的已完成60F前缀缺根或时点不符")
    return filtered


def collect(day, cutoff="11:30", workers=4, limit=0):
    cfg = get_config()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if cutoff not in SLOTS or day != str(now.date()) or pd.Timestamp(f"{day} {cutoff}") > pd.Timestamp(now).tz_localize(None):
        raise ValueError("仅采集今天已经完成的原生60F时点")
    folder = Path(cfg.root_dir) / "output" / "market" / "intraday_watch" / f"{day}_{cutoff.replace(':', '')}"
    folder.mkdir(parents=True, exist_ok=True)
    with file_lock(folder / "collection.lock") as acquired:
        if not acquired:
            raise RuntimeError("已有同批盘中采集在运行")
        snapshot = load_dashboard(include_rows=False)
        manifest = read_json(folder / "collection.json", strict=True)
        if manifest:
            members = manifest["universe"]
        else:
            members = [m for m in scan_universe(day, listed=True) if not risk_name(m.get("name"))]
            if limit:
                members = members[:limit]
            manifest = {"day": day, "cutoff": cutoff, "snapshot_run_id": snapshot["run_id"], "universe": members}
            write_json(folder / "collection.json", manifest)
        factors = fetch_adjustments(day=day)
        if factors.empty:
            raise ValueError("今日真实因子尚不可得，未使用昨日因子填充")
        save_adjustments(factors)
        factor_codes = set(factors.ts_code)
        outcomes, pending = [], []
        for member in members:
            cached = read_json(folder / "bars" / (member["ts_code"] + ".json"), strict=True)
            if cached.get("status") == "ok":
                outcomes.append(cached)
            else:
                pending.append(member)

        def one(member):
            code = member["ts_code"]
            outcome = {"ts_code": code, "status": "error", "rows": 0}
            for attempt in range(2):
                try:
                    if code not in factor_codes:
                        raise ValueError("今日真实复权因子缺失")
                    raw = fetch_sina_history(code, day, day, limit=8)
                    clipped = closed_prefix(raw, day, cutoff, now)
                    save_closed_prefix(clipped)
                    outcome = {"ts_code": code, "status": "ok", "rows": len(clipped),
                               "last_bar": str(clipped.bar_end.max()),
                               "discarded_later": int((pd.to_datetime(raw.bar_end) > pd.Timestamp(f"{day} {cutoff}")).sum())}
                    break
                except Exception as error:
                    outcome["error_type"] = type(error).__name__
                    if attempt == 0:
                        time.sleep(1)
            write_json(folder / "bars" / (code + ".json"), outcome)
            time.sleep(.2)
            return outcome

        logger.info("盘中采集 %s %s: 股票池%s，缓存%s，待采集%s", day, cutoff, len(members), len(outcomes), len(pending))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, member) for member in pending]
            for future in as_completed(futures):
                outcomes.append(future.result())
                if len(outcomes) % 100 == 0 or len(outcomes) == len(members):
                    progress = {"completed": len(outcomes), "total": len(members), "ok": sum(r["status"] == "ok" for r in outcomes), "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
                    write_json(folder / "progress.json", progress)
                    logger.info("盘中采集进度 %s/%s，完整%s", progress["completed"], progress["total"], progress["ok"])
        result = {"day": day, "cutoff": cutoff, "total": len(members), "complete": sum(r["status"] == "ok" for r in outcomes),
                  "rows": sum(r["rows"] for r in outcomes), "failed": [r for r in outcomes if r["status"] != "ok"],
                  "discarded_later": sum(r.get("discarded_later", 0) for r in outcomes)}
        write_json(folder / "audit.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description="采集今天已完成的60F前缀，不推进盘后同步状态")
    parser.add_argument("--day", default=str(datetime.now(ZoneInfo("Asia/Shanghai")).date()))
    parser.add_argument("--cutoff", choices=SLOTS, default="11:30")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(collect(args.day, args.cutoff, args.workers, args.limit), default=str))


if __name__ == "__main__":
    main()
