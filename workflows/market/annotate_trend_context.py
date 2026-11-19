"""Publish additive daily parent-wave context without rescanning entry rules."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from storage.pg import retry_on_disconnect
from skills.trend.parent_context import annotate_summary, ParentConfig, VERSION
from tools.market.trend_data import load_dashboard, scan_daily_inputs, adjust_prices
from storage.repositories.trend import publish

logger = get_logger(__name__)


def annotate_batch(rows, start, end):
    daily = retry_on_disconnect(
        lambda: scan_daily_inputs([r["ts_code"] for r in rows], start, end)
    )
    groups = dict(tuple(daily.groupby("ts_code"))) if not daily.empty else {}
    results = []
    for row in rows:
        frame, issue = adjust_prices(groups.get(row["ts_code"], pd.DataFrame()))
        if issue:
            frame = pd.DataFrame()
        result = annotate_summary(row, frame, end)
        if issue:
            result["parent_context_issue"] = issue
        # Every field from the original signal remains byte-value equivalent.
        assert all(
            result[key] == value
            for key, value in row.items()
            if not key.startswith(("parent_", "context_pattern", "local_trigger"))
        )
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish-only", action="store_true")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    folder = Path(get_config().root_dir) / "output/trend/parent_context_v1"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "snapshot.json"
    if args.publish_only:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    else:
        original = retry_on_disconnect(lambda: load_dashboard(None, True))
        if original is None:
            raise ValueError("尚无可标注的趋势快照")
        metadata = deepcopy(original["metadata"])
        end = (
            metadata.get("timeframes", {}).get("1D", {}).get("as_of", original["as_of"])
        )
        end = min(pd.Timestamp(end), pd.Timestamp(original["as_of"])).isoformat()
        batches = [
            original["data"][i : i + 100] for i in range(0, len(original["data"]), 100)
        ]
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for chunk in pool.map(
                lambda b: annotate_batch(b, metadata["start"], end), batches
            ):
                results.extend(chunk)
                logger.info("Parent context %s/%s", len(results), len(original["data"]))
        results.sort(key=lambda r: r["ts_code"])
        metadata.update(
            parent_context_version=VERSION,
            parent_context_config=asdict(ParentConfig()),
            parent_context_source_run_id=original["run_id"],
            opportunity_patterns=dict(
                Counter(
                    r.get("context_pattern") or r.get("pattern", "")
                    for r in results
                    if r["eligible"]
                )
            ),
        )
        snapshot = dict(
            run_id=str(uuid4()),
            as_of=original["as_of"],
            version=original["version"],
            metadata=metadata,
            data=results,
        )
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    existing = retry_on_disconnect(lambda: load_dashboard(snapshot["run_id"], False))
    if existing is None:
        retry_on_disconnect(
            lambda: publish(
                snapshot["run_id"],
                snapshot["as_of"],
                snapshot["version"],
                snapshot["metadata"],
                snapshot["data"],
            )
        )
    print(
        json.dumps(
            dict(
                run_id=snapshot["run_id"],
                rows=len(snapshot["data"]),
                source_run_id=snapshot["metadata"]["parent_context_source_run_id"],
                patterns=snapshot["metadata"]["opportunity_patterns"],
            )
        )
    )


if __name__ == "__main__":
    main()
