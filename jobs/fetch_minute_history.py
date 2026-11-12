"""Export historical minute bars to CSV without touching PG or starting schedulers.

Example: python -m jobs.fetch_minute_history --code 600000.SH --periods 30 60
SDK calls run in a separate process so a stuck network request can be terminated.
"""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import time

from tools.market.minute_history import SOURCES


def main() -> int:
    parser = argparse.ArgumentParser(description="历史分钟K线导出（不会写入数据库）")
    parser.add_argument("--source", choices=SOURCES, default="akshare-sina")
    parser.add_argument("--code", required=True, help="例如 600000.SH")
    parser.add_argument("--periods", type=int, nargs="+", choices=[5, 15, 30, 60], default=[30, 60])
    parser.add_argument("--start", help="YYYY-MM-DD 或日期时间；BaoStock 必填")
    parser.add_argument("--end", help="同上，日期包含全天；缺省到当前时间")
    parser.add_argument("--adjustment", choices=["none", "forward", "backward"], default="none")
    parser.add_argument("--timeout", type=float, default=45, help="每个周期的完整进程超时秒数")
    parser.add_argument("--output", type=Path, default=Path("output/market/minute_history"))
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout 必须大于零")
    if args._worker:
        from tools.market.minute_history import fetch_minute_history
        try:
            # Some SDKs print to stdout; isolation makes redirection safe here.
            with contextlib.redirect_stdout(sys.stderr):
                frame = fetch_minute_history(args.source, args.code, args.periods[0],
                                             start=args.start, end=args.end,
                                             adjustment=args.adjustment)
            payload = {"rows": frame.to_dict(orient="records")}
            print(json.dumps(payload, ensure_ascii=False, default=str, allow_nan=True))
            return 0
        except Exception as error:
            print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
            return 1

    from datetime import datetime
    from zoneinfo import ZoneInfo
    import pandas as pd

    # Separate run directories preserve data and provenance from previous requests.
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S%f")
    directory = args.output.resolve() / stamp
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for period in dict.fromkeys(args.periods):
        record = {"source": args.source, "code": args.code, "period": period,
                  "start": args.start, "end": args.end, "adjustment": args.adjustment}
        command = [sys.executable, "-X", "utf8", "-m", "jobs.fetch_minute_history", "--_worker",
                   "--source", args.source, "--code", args.code, "--periods", str(period),
                   "--adjustment", args.adjustment]
        for key in ("start", "end"):
            if getattr(args, key):
                command.extend([f"--{key}", getattr(args, key)])
        began = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", timeout=args.timeout)
            payload = json.loads(result.stdout)
            if result.returncode or "error" in payload:
                raise RuntimeError(payload.get("error", "worker failed"))
            frame = pd.DataFrame(payload["rows"])
            record.update(rows=len(frame), status="empty" if frame.empty else "ok")
            if not frame.empty:
                # Filename contains no untrusted code or supplier-provided path.
                target = directory / f"bars_{period}.csv"
                frame.to_csv(target, index=False, encoding="utf-8-sig")
                record.update(csv=str(target), first_time=frame.bar_end.min(), last_time=frame.bar_end.max())
                if args.source.startswith("akshare"):
                    record["coverage_note"] = "滚动窗口；返回起止时间不代表请求区间已完整覆盖"
        except subprocess.TimeoutExpired:
            record.update(status="error", error=f"采集进程超过 {args.timeout:g} 秒，已终止")
        except Exception as error:
            record.update(status="error", error=str(error)[:500])
        record["elapsed_s"] = round(time.monotonic() - began, 2)
        record["completed_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        records.append(record)
        print(json.dumps(record, ensure_ascii=False))
    (directory / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if any(r["status"] != "ok" for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
