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

from core.logger import get_logger
logger = get_logger(__name__)

from core.config import get_config
from tools.zsxq.downloader import FileDownloader
from storage.sqlite.files import FilesDatabase
from core.paths import PathManager


def _process_one_report(group_id: str) -> str:
    """
    单篇研报入库流水线: PG 同步(增量, 拿到新入库 report_id)
      → 新研报逐篇 [LLM 元数据 + 深度提取(评级/盈利预测/tags)]
      → 去重合并.
    在每个文件下载完成后立即调用, 不等整批结束 —— 深度提取跟上下载节奏,
    每篇处理完即 analysis_status='done', 前端立即可用.
    (图片型 PDF 由深度提取转后台 OCR, OCR 完成后自动接续提取, 见 ReportSkill)
    返回 'ok' / 'noop' / 'error: ...' / 'ok+extracted N'
    """
    new_ids: list = []
    try:
        from jobs.sync_zxsq_to_pg import ZSXQ2PGSync
        sync = ZSXQ2PGSync(str(group_id))
        new_ids = sync.run() or []  # 增量水位线: 只处理新下载的文件, 秒级
    except Exception as e:
        return f"error: pg_sync {e}"

    extracted = 0
    try:
        if new_ids:
            # 只对刚入库的 PDF/docx 做单篇 LLM 分析 (精准定位, 不受旧积压影响);
            # 话题文本条目 (txt) 由 merge 归并进同话题 PDF, 不单独消耗 LLM
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
                        analyze_report_meta(rid)  # 元数据秒级 (仅文件名)
                        ctx = ReportSkill()(SkillContext(user_input="", params={
                            "mode": "extract", "report_id": rid, "limit": 1}))
                        if ctx.error:
                            logger.warning(f"研报 #{rid} 深度提取失败: {ctx.error}")
                        else:
                            extracted += 1
                    except Exception as e:
                        logger.warning(f"研报 #{rid} 单篇分析失败: {e}")
        else:
            # 无新文件: 顺手清理积压元数据 (最旧优先)
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
    """执行一次抓取, 返回统计"""
    cookie = get_config().zsxq_cookie
    if not cookie:
        logger.error("未配置 ZSXQ_COOKIE")
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

    # 2. 下载待下载文件 (逐个: 下载一个 → 立即分析入库; 长休眠与分析并行覆盖)
    before_stats = dl.db.get_stats()
    pending_before = before_stats.get("pending", 0)
    result["pending_before"] = pending_before

    processed = 0
    if pending_before > 0 and max_fetch > 0:
        # 关闭下载器内置的"每文件后长休眠", 由本循环控制节奏:
        # 下载完立即入库分析, 分析耗时被长休眠自然覆盖
        dl.long_sleep_min = 60
        dl.long_sleep_max = 120

        n_done = 0
        while n_done < max_fetch:
            # 先重试此前失败的任务 (每轮最多 3 个, fail_count<=5), 再下新文件
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

            # 逐个下载 (download_pending 内部会按 pending 状态取)
            stats = dl.download_pending(max_files=1)
            if stats.get("total", 0) == 0:
                break
            n_done += 1
            # 立即处理刚下载的文件
            status = _process_one_report(group_id)
            processed += 1
            logger.info(f"🔬 已入库分析 {processed} 篇 ({status})")

    after_stats = dl.db.get_stats()
    result["download"] = after_stats
    result["processed_inline"] = processed

    dl.close()

    # 3. 收尾: 兜底同步 + 深度提取 (拿到本轮漏网/去重后的研报)
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