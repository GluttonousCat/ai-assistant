# -*- encoding: utf-8 -*-
"""Collect, download and ingest report attachments; queue their model analysis."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from core.logger import get_logger
from core.observability import observed, task_progress

logger = get_logger(__name__)

from core.config import get_config
from tools.zsxq.downloader import FileDownloader
from core.paths import PathManager


def _process_one_report(group_id: str) -> str:
    """Ingest the ready attachment and register its paid work."""
    from workflows.reports.sync import ZSXQ2PGSync
    from workflows.reports.deduplicate import merge

    try:
        task_progress("同步下载文件至研报库")
        new_ids = ZSXQ2PGSync(str(group_id), ready_limit=1).run() or []
        task_progress("合并重复报告")
        merge()
    except Exception as exc:
        return f"error: report ingestion ({exc})"
    return "ok" if new_ids else "noop"


@observed("reports_fetch", "研报抓取与逐篇入库")
def run_once(
    group_id: str,
    max_fetch: int = 10,
    max_pages: int = 10,
    download_only_files: bool = False,
    crawl_topics: bool = True,
) -> dict:
    """Run one fetch round; returns statistics."""
    cookie = get_config().zsxq_cookie
    if not cookie:
        logger.error("未配置 ZSXQ_COOKIE")
        return {"error": "no cookie"}

    result = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "group": group_id}

    # 0. Also crawl the latest topics (research-report core-view text lives in the
    #    topics; crawl daily to stay in sync)
    if crawl_topics:
        task_progress("抓取最新话题")
        from tools.zsxq.crawler import ZSXQCrawler

        try:
            crawler = ZSXQCrawler(cookie, str(group_id))
            # Topic crawl intervals come from config (zsxq.crawl_interval_min/max),
            # no longer hardcoded
            _cfg = get_config()
            crawler.set_custom_intervals(
                crawl_interval_min=float(_cfg.get("zsxq.crawl_interval_min", 1.5)),
                crawl_interval_max=float(_cfg.get("zsxq.crawl_interval_max", 2.5)),
            )
            result["topics"] = crawler.crawl_latest(count=20)
            crawler.close()
        except Exception as e:
            result["topics"] = {"error": str(e)}
            task_progress(warning=f"话题抓取失败: {e}")

    dl = FileDownloader(cookie, str(group_id))

    # 1. Collect the file list
    if not download_only_files:
        task_progress("收集待下载文件清单")
        collect = dl.collect_files(max_pages=max_pages)
        result["collect"] = collect

    # Download one attachment at a time and persist its pending analysis.
    before_stats = dl.db.get_stats()
    pending_before = before_stats.get("pending", 0)
    result["pending_before"] = pending_before

    if max_fetch > 0:
        retry = dl.db.get_retry_files(limit=3)
        for row in retry:
            dl.db.update_status(row[0], "pending")
        if retry:
            dl.db.commit()
            pending_before += len(retry)
            logger.info("本轮重试 %s 个下载任务", len(retry))

    processed = 0
    if pending_before > 0 and max_fetch > 0:
        # FileDownloader applies the configured download intervals.
        n_done = 0
        while n_done < max_fetch:
            task_progress(
                "下载研报（含反检测等待间隔）",
                completed=n_done,
                total=min(max_fetch, pending_before),
            )
            # Download one by one (download_pending internally picks by pending status)
            stats = dl.download_pending(max_files=1)
            if stats.get("failed", 0):
                task_progress(warning="本轮存在下载失败，请查看采集日志")
            if stats.get("total", 0) == 0:
                break
            n_done += 1
            # Immediately process the just-downloaded file
            task_progress(
                "逐篇入库、排队与去重",
                completed=n_done,
                total=min(max_fetch, pending_before),
            )
            status = _process_one_report(group_id)
            if status.startswith("error"):
                task_progress(warning=status)
            processed += 1
            logger.info(f"🔬 已处理下载 {processed} 篇 ({status})")

    after_stats = dl.db.get_stats()
    result["download"] = after_stats
    result["processed_inline"] = processed

    dl.close()

    # Sync ready attachments left by an earlier interruption.
    try:
        task_progress("收尾：补齐研报入库")
        from workflows.reports.sync import ZSXQ2PGSync

        sync = ZSXQ2PGSync(str(group_id), ready_limit=1)
        sync.run()
        result["pg_sync"] = "ok"
    except Exception as e:
        result["pg_sync"] = f"error: {e}"
        task_progress(warning=f"收尾入库失败: {e}")

    return result


def main():
    parser = argparse.ArgumentParser(description="每日定时抓取知识星球文件")
    parser.add_argument(
        "--group", default=None, help="群组ID (默认 .env ZSXQ_GROUP_ID)"
    )
    parser.add_argument("--max-fetch", type=int, default=10, help="每次下载文件数上限")
    parser.add_argument("--pages", type=int, default=10, help="收集文件列表页数")
    parser.add_argument(
        "--check", action="store_true", help="仅检查当天新增文件, 不下载"
    )
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
        (today,),
    )
    rows = db.cursor.fetchall()
    print(f"\n今天 ({today}) 新增 {len(rows)} 个文件:")
    for r in rows[:30]:
        print(f"  [{r[0]}] {r[1][:60]} ({r[2] // 1024}KB) {r[3]}")
    db.close()


if __name__ == "__main__":
    main()
