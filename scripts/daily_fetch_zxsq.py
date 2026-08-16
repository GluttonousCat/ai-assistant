# -*- encoding: utf-8 -*-
"""
每日定时抓取知识星球文件 (供 cron/计划任务每 6 小时调用一次)

用途: 猫哥的研报圈等以文件为主的群组, 每天 6/12/18/24 四时段将当天新增文件下载到本地.

流程:
  1. 收集文件列表 (增量, 只新增不在库中的文件)
  2. 下载待下载文件 (每次限 N 个, 使用反检测强制间隔)

用法:
  python -m scripts.daily_fetch_zxsq --group 51288148188224 --max-fetch 10
  python -m scripts.daily_fetch_zxsq --group 51288148188224 --max-fetch 10 --pages 5

Windows 定时任务 (schtasks) / Linux cron 示例见文件末尾注释.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import get_config
from tools.zsxq.downloader import FileDownloader
from storage.sqlite.files import FilesDatabase
from utils.paths import PathManager


def run_once(group_id: str, max_fetch: int = 10, max_pages: int = 10,
             download_only_files: bool = False, crawl_topics: bool = True) -> dict:
    """执行一次抓取, 返回统计"""
    cookie = get_config().zsxq_cookie
    if not cookie:
        print("❌ 未配置 ZSXQ_COOKIE")
        return {"error": "no cookie"}

    result = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "group": group_id}

    # 0. 顺带爬最新话题 (研报核心观点文本都附在话题里, 每天更新保持同步)
    if crawl_topics:
        from tools.zsxq.crawler import ZSXQCrawler
        try:
            crawler = ZSXQCrawler(cookie, str(group_id))
            crawler.set_custom_intervals(crawl_interval_min=1.5, crawl_interval_max=2.5)
            result["topics"] = crawler.crawl_latest(count=20)
            crawler.close()
        except Exception as e:
            result["topics"] = {"error": str(e)}

    dl = FileDownloader(cookie, str(group_id))

    # 1. 收集文件列表
    if not download_only_files:
        collect = dl.collect_files(max_pages=max_pages)
        result["collect"] = collect

    # 2. 下载待下载文件 (限制数量, 避免单次运行过长)
    before_stats = dl.db.get_stats()
    pending_before = before_stats.get("pending", 0)
    result["pending_before"] = pending_before

    if pending_before > 0 and max_fetch > 0:
        dl.download_pending(max_files=max_fetch)

    after_stats = dl.db.get_stats()
    result["download"] = after_stats

    dl.close()

    # 3. 同步到 PG 研报库
    try:
        from scripts.sync_zxsq_to_pg import ZSXQ2PGSync
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
        print("❌ 未指定群组ID (--group 或 .env ZSXQ_GROUP_ID)")
        sys.exit(1)

    if args.check:
        # 只检查: 列出当天文件
        _check_today(group_id)
        return

    result = run_once(group_id, max_fetch=args.max_fetch, max_pages=args.pages)

    print("\n=== 抓取结果 ===")
    for k, v in result.items():
        print(f"  {k}: {v}")

    if "error" in result:
        sys.exit(1)


def _check_today(group_id: str):
    """检查今天新增的文件 (不下载)"""
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

# =========================================================================
# 定时任务配置 (schtasks / cron)
# =========================================================================
# Windows 定时任务 (schtasks) — 每天 6/12/18/24 点:
#   schtasks /Create /TN "ZSXQ每日抓取6" /TR "C:\Users\...\.venv\Scripts\python.exe C:\Users\...\scripts\daily_fetch_zxsq.py --group 51288148188224 --max-fetch 10" /SC DAILY /ST 06:00
#   schtasks /Create /TN "ZSXQ每日抓取12" /TR "..." /SC DAILY /ST 12:00
#   schtasks /Create /TN "ZSXQ每日抓取18" /TR "..." /SC DAILY /ST 18:00
#   schtasks /Create /TN "ZSXQ每日抓取24" /TR "..." /SC DAILY /ST 00:00
#
# Linux cron:
#   0 6,12,18,0 * * * cd /path/ai-assistant && .venv/bin/python -m scripts.daily_fetch_zxsq --group 51288148188224 --max-fetch 10 >> logs/daily_fetch.log 2>&1