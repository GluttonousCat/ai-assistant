"""Explicit, resumable research download; never invoked by an HTTP request.

Uses the same unadjusted Sina source as the warehouse. Daily factors and price
limits remain the frozen warehouse inputs; no data or waterline is written back.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import pickle
import time

from core.config import get_config
from core.logger import get_logger
from tools.market.sina_transport import fetch_sina_history

logger = get_logger(__name__)


def is_mainboard(code):
    return (code.endswith(".SH") and code.startswith(("600", "601", "603", "605"))) or (
        code.endswith(".SZ") and code.startswith(("000", "001", "002", "003"))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.workers <= 6:
        parser.error("workers must be 1..6")
    root = Path(get_config().root_dir)
    folder = root / "output/quant/mainboard_60f_v8"
    cache = folder / "minutes"
    cache.mkdir(parents=True, exist_ok=True)
    with (root / "output/quant/reversal_v6_1/prepared.pkl").open("rb") as stream:
        payload = pickle.load(stream)  # Project-generated trusted local cache.
    members = [
        m
        for m in payload["members"]
        if is_mainboard(m["ts_code"])
        and "ST" not in m["name"].upper()
        and "退" not in m["name"]
    ]
    codes = [m["ts_code"] for m in members]
    pending = [c for c in codes if not (cache / f"{c}.pkl").exists()]
    logger.info(
        "Mainboard research cache: total=%s pending=%s", len(codes), len(pending)
    )

    def download(code):
        last = None
        for attempt in range(3):
            try:
                frame = fetch_sina_history(code, "2026-01-01", "2026-11-12")
                if frame.empty:
                    raise ValueError("empty source history")
                tmp = cache / f"{code}.part"
                frame.to_pickle(tmp)
                tmp.replace(cache / f"{code}.pkl")
                time.sleep(0.2)
                return None
            except Exception as exc:
                last = type(exc).__name__
                time.sleep(0.5 * (attempt + 1))
        return {"ts_code": code, "error": last}

    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download, c): c for c in pending}
        for n, future in enumerate(as_completed(futures), 1):
            error = future.result()
            if error:
                errors.append(error)
            if n % 100 == 0 or n == len(pending):
                logger.info(
                    "Minute cache %s/%s errors=%s", n, len(pending), len(errors)
                )
    manifest = dict(
        source="akshare-sina",
        adjustment="none",
        start="2026-01-01",
        end="2026-11-12",
        members=codes,
        errors=errors,
        cached=sum((cache / f"{c}.pkl").exists() for c in codes),
        universe="frozen five-index union, mainboard, current non-ST",
    )
    (folder / "cache_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in manifest.items() if k != "members"}))


if __name__ == "__main__":
    main()
