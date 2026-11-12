# -*- encoding: utf-8 -*-
"""
Daily scheduled fetch of Zhixingqiu (knowledge-planet) files.

Purpose: for file-centric groups such as Maoge's research-report circle, download each day's new files locally at the two daily slots 07:00/23:00.

Flow:
  1. Collect the file list (incremental; only add files not yet in the DB)
  2. Download pending files (limit N per run, using anti-detection forced intervals)

Usage:
  python -m jobs.daily_fetch_zxsq --group 51288148188224 --max-fetch 10
  python -m jobs.daily_fetch_zxsq --group 51288148188224 --max-fetch 10 --pages 5

Windows scheduled task (schtasks) / Linux cron examples: see the comments at the end of this file.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logger import get_logger
logger = get_logger(__name__)

from core.config import get_config
from tools.zsxq.downloader import FileDownloader
from core.paths import PathManager


def _process_one_report(group_id: str) -> str:
    """
    Per-report ingestion pipeline: PG sync (incremental, returns newly ingested report_ids)
      → for each new report: [LLM metadata + deep extraction (rating/forecasts/tags)]
      → dedup merge.
    Called immediately after each file finishes downloading, not after the whole batch —
    deep extraction keeps pace with the download cadence; each report becomes
    analysis_status='done' as soon as processed and is immediately usable in the frontend.
    (Image-only PDFs are routed by deep extraction to background OCR; extraction resumes
    automatically after OCR completes — see ReportSkill)
    Returns 'ok' / 'noop' / 'error: ...' / 'ok+extracted N'
    """
    new_ids: list = []
    try:
        from jobs.sync_zxsq_to_pg import ZSXQ2PGSync
        sync = ZSXQ2PGSync(str(group_id))
        new_ids = sync.run() or []  # Incremental watermark: only newly downloaded files are processed, seconds-fast
    except Exception as e:
        return f"error: pg_sync {e}"

    extracted = 0
    try:
        if new_ids:
            # Run per-report LLM analysis only on just-ingested PDF/docx (precisely
            # targeted, unaffected by old backlog);
            # topic text entries (txt) are merged by merge() into the same topic's PDF
            # and do not consume LLM calls separately
            from storage.pg import PgClient
            with PgClient() as pg:
                rows = pg.fetch_all(
                    "SELECT report_id, file_name FROM fin.report_meta "
                    "WHERE report_id = ANY(%s)", (list(new_ids),))
            docs = [r for r in rows if (r.get("file_name") or "").lower()
                    .endswith((".pdf", ".docx", ".doc"))]
            if docs:
                from skills.base import SkillContext
                from skills.report.skill import ReportSkill
                from tools.finance.report_meta_analysis import analyze_report_meta
                for r in docs:
                    rid = r["report_id"]
                    try:
                        analyze_report_meta(rid)  # Metadata takes seconds (filename only)
                        ctx = ReportSkill()(SkillContext(user_input="", params={
                            "mode": "extract", "report_id": rid, "limit": 1}))
                        if ctx.error:
                            logger.warning(f"研报 #{rid} 深度提取失败: {ctx.error}")
                        else:
                            extracted += 1
                    except Exception as e:
                        logger.warning(f"研报 #{rid} 单篇分析失败: {e}")
        else:
            # No new files: opportunistically clear pending metadata (oldest first)
            from tools.finance.report_meta_analysis import analyze_pending
            analyze_pending(limit=5)
    except Exception as e:
        return f"error: analysis ({e})"

    try:
        from jobs.merge_report_duplicates import merge
        merge()
    except Exception as e:
        return f"error: merge {e}"
    if not new_ids:
        return "noop"
    return "ok" + (f"+extracted {extracted}" if extracted else "")


def run_once(group_id: str, max_fetch: int = 10, max_pages: int = 10,
             download_only_files: bool = False, crawl_topics: bool = True) -> dict:
    """Run one fetch round; returns statistics."""
    cookie = get_config().zsxq_cookie
    if not cookie:
        logger.error("未配置 ZSXQ_COOKIE")
        return {"error": "no cookie"}

    result = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "group": group_id}

    # 0. Also crawl the latest topics (research-report core-view text lives in the
    #    topics; crawl daily to stay in sync)
    if crawl_topics:
        from tools.zsxq.crawler import ZSXQCrawler
        try:
            crawler = ZSXQCrawler(cookie, str(group_id))
            # Topic crawl intervals come from config (zsxq.crawl_interval_min/max),
            # no longer hardcoded
            _cfg = get_config()
            crawler.set_custom_intervals(
                crawl_interval_min=float(_cfg.get("zsxq.crawl_interval_min", 1.5)),
                crawl_interval_max=float(_cfg.get("zsxq.crawl_interval_max", 2.5)))
            result["topics"] = crawler.crawl_latest(count=20)
            crawler.close()
        except Exception as e:
            result["topics"] = {"error": str(e)}

    dl = FileDownloader(cookie, str(group_id))

    # 1. Collect the file list
    if not download_only_files:
        collect = dl.collect_files(max_pages=max_pages)
        result["collect"] = collect

    # 2. Download pending files (one by one: download one → immediately ingest and
    #    analyze; long sleeps run in parallel with, and cover, the analysis time)
    before_stats = dl.db.get_stats()
    pending_before = before_stats.get("pending", 0)
    result["pending_before"] = pending_before

    processed = 0
    if pending_before > 0 and max_fetch > 0:
        # Long-sleep intervals are initialized by FileDownloader from config
        # (zsxq.download_*); analysis time is naturally covered by the long sleeps —
        # no hardcoded overrides here anymore
        n_done = 0
        while n_done < max_fetch:
            # First retry previously failed tasks (at most 3 per round, fail_count<=5),
            # then download new files
            retried = 0
            try:
                for row in dl.db.get_retry_files(limit=3):
                    if dl.is_stopped():
                        break
                    file_id, name, size, download_count, create_time = row
                    dl.db.update_status(file_id, 'pending')
                    retried += 1
                if retried:
                    logger.info(f"🔁 重置 {retried} 个失败任务重试")
            except Exception as e:
                logger.warning(f"重试重置失败: {e}")

            # Download one by one (download_pending internally picks by pending status)
            stats = dl.download_pending(max_files=1)
            if stats.get("total", 0) == 0:
                break
            n_done += 1
            # Immediately process the just-downloaded file
            status = _process_one_report(group_id)
            processed += 1
            logger.info(f"🔬 已入库分析 {processed} 篇 ({status})")

    after_stats = dl.db.get_stats()
    result["download"] = after_stats
    result["processed_inline"] = processed

    dl.close()

    # 3. Wrap-up: fallback sync + deep extraction (pick up reports missed this round
    #    or produced by dedup)
    try:
        from jobs.sync_zxsq_to_pg import ZSXQ2PGSync
        sync = ZSXQ2PGSync(str(group_id))
        sync.run()
        result["pg_sync"] = "ok"
    except Exception as e:
        result["pg_sync"] = f"error: {e}"

    return result


def main():
    parser = argparse.ArgumentParser(description="每日定时抓取知识星球文件")
    parser.add_argument("--group", default=None, help="群组ID (默认 .env ZSXQ_GROUP_ID)")
    parser.add_argument("--max-fetch", type=int, default=10, help="每次下载文件数上限")
    parser.add_argument("--pages", type=int, default=10, help="收集文件列表页数")
    parser.add_argument("--check", action="store_true",
                        help="仅检查当天新增文件, 不下载")
    args = parser.parse_args()

    group_id = args.group or get_config().zsxq_group_id
    if not group_id:
        logger.error("未指定群组ID (--group 或 .env ZSXQ_GROUP_ID)")
        sys.exit(1)

    if args.check:
        # Check only: list today's files
        _check_today(group_id)
        return

    result = run_once(group_id, max_fetch=args.max_fetch, max_pages=args.pages)

    print("\n=== 抓取结果 ===")
    for k, v in result.items():
        print(f"  {k}: {v}")

    if "error" in result:
        sys.exit(1)


def _check_today(group_id: str):
    """Check files added today (without downloading)."""
    pm = PathManager()
    db_path = pm.get_files_db_path(str(group_id))
    if not os.path.exists(db_path):
        print("文件库不存在, 请先运行收集")
        return

    from storage.sqlite.files import FilesDatabase
    db = FilesDatabase(db_path)
    today = datetime.now().strftime("%Y-%m-%d")
    db.cursor.execute(
        "SELECT file_id, name, size, create_time FROM files WHERE substr(create_time,1,10)=? ORDER BY create_time DESC",
        (today,))
    rows = db.cursor.fetchall()
    print(f"\n今天 ({today}) 新增 {len(rows)} 个文件:")
    for r in rows[:30]:
        print(f"  [{r[0]}] {r[1][:60]} ({r[2] // 1024}KB) {r[3]}")
    db.close()


if __name__ == "__main__":
    main()
