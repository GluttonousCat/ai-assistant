"""
存量研报 -> 产业链环节抽取回填 (fin.chain_extract)

选片: 近 N 天、已有正文、且尚未抽取过的研报 (倒序 newest-first)
幂等: 重跑只处理未覆盖的 (如需重抽单篇: DELETE FROM fin.chain_extract WHERE report_id=X)

用法:
    python -m scripts.backfill_chain_extract            # 近90天, 最多100篇
    python -m scripts.backfill_chain_extract --days 180 --limit 300
"""
from __future__ import annotations

import argparse
import time

from core.logger import get_logger
from storage.pg import PgClient
from beta_alpha.schema import init_chain_extract_schema
from beta_alpha.analysis.chain_extract import extract_chain

logger = get_logger("backfill_chain_extract")

LLM_SLEEP = 2.0  # 单篇一次 LLM 调用, 留间隔防限流


def pick_reports(days: int, limit: int):
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT rm.report_id, rm.title FROM fin.report_meta rm "
            "WHERE rm.content_text IS NOT NULL AND length(rm.content_text) > 200 "
            "  AND rm.publish_date >= current_date - %s::int "
            "  AND NOT EXISTS (SELECT 1 FROM fin.chain_extract ce "
            "                  WHERE ce.report_id = rm.report_id) "
            "ORDER BY rm.publish_date DESC LIMIT %s",
            (days, limit),
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="存量研报链抽取回填")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    with PgClient() as pg:
        init_chain_extract_schema(pg)

    reports = pick_reports(args.days, args.limit)
    if not reports:
        print(f"近 {args.days} 天无待抽取研报 (limit={args.limit})")
        return
    print(f"待抽取: {len(reports)} 篇 (近{args.days}天)")

    from beta_alpha.prompts import CHAIN_EXTRACT_PROMPT
    done = total_segs = 0
    for i, r in enumerate(reports, 1):
        try:
            n = extract_chain(r["report_id"], CHAIN_EXTRACT_PROMPT)
            done += 1
            total_segs += n
            print(f"[{i}/{len(reports)}] {str(r['title'])[:40]} -> {n} 环节")
        except Exception as e:
            print(f"[{i}/{len(reports)}] report_id={r['report_id']} 失败: {e}")
        time.sleep(LLM_SLEEP)
    print(f"回填完成: {done}/{len(reports)} 篇, 累计环节 {total_segs} 行")


if __name__ == "__main__":
    main()
