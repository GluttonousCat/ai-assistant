"""Repair missing factors for imported minutes or native daily bars; never infer values.

Common missing dates use a market-wide query; isolated stocks (including BJ and
ST names absent from the old daily whitelist) use bounded single-code queries.
Does not advance daily sync watermarks, since this is not a full daily sync.
"""

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from core.config import get_config
from core.logger import get_logger
from storage.repositories.trend import missing_adjustments, missing_daily_adjustments, save_adjustments
from tools.market.adjustments import fetch_adjustments

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="补齐已入库分钟/原生日K对应的真实复权因子")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--daily", action="store_true", help="补齐原生日K对应因子；不更新同步水位线")
    parser.add_argument(
        "--end", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    )
    args = parser.parse_args()
    get_config()
    read_gaps = missing_daily_adjustments if args.daily else missing_adjustments
    before = read_gaps(args.start, args.end)
    counts = Counter(d for r in before for d in r["dates"])
    dates = sorted(d for d, n in counts.items() if n >= 100)
    written = 0
    for day in dates:
        frame = fetch_adjustments(day=day)
        save_adjustments(frame)
        written += len(frame)
        logger.info("补齐复权因子 %s: %s行", day, len(frame))
        time.sleep(0.4)
    remaining = read_gaps(args.start, args.end)
    for i, row in enumerate(remaining, 1):
        code = row["ts_code"]
        frame = fetch_adjustments(code=code, start=args.start, end=args.end)
        save_adjustments(frame)
        written += len(frame)
        logger.info(
            "补齐个股复权因子 %s/%s %s: %s行", i, len(remaining), code, len(frame)
        )
        time.sleep(0.4)
    after = read_gaps(args.start, args.end)
    result = {
        "start": args.start,
        "end": args.end,
        "before_stocks": len(before),
        "written": written,
        "missing_stocks": len(after),
        "remaining": [
            {"ts_code": r["ts_code"], "missing_days": len(r["dates"])} for r in after
        ],
    }
    folder = Path(get_config().root_dir) / "output" / "market" / "trend"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("factor_repair_daily.json" if args.daily else "factor_repair.json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if after else 0


if __name__ == "__main__":
    raise SystemExit(main())
