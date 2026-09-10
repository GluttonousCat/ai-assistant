# -*- encoding: utf-8 -*-
"""
研报去重合并: 知识星球同一研报常有多条记录 —
    source='zsxq' (话题附件: PDF 原研报, 可能正文为空)
    source='zsxq_topic' (话题文本: 中文解读 txt)
    source='zsxq_file' (群文件: 含 mp3 音频)
规则:
    1. 每个 topic 保留 PDF 条目 (正文优先 PDF 提取文本, 空则并入话题解读文本);
       保留优先级: 非中文翻译版 > 已分析(done) > 新记录 —— 删除对象集中在
       中文版与未分析条目 (中文版与原版成对时只留原版);
    2. 同 topic 的 zsxq_topic (txt) / zsxq_file(mp3) 条目删除 (内容已并入);
    3. 无 PDF 的 topic: 保留 txt 解读条目 (source 改 'zsxq_topic_pdf_absent',
       报表 API 仍不展示, 仅作溯源)。
留痕 (防乒乓循环, 2026-09-03): 删除带 file_id 的条目前写入 fin.report_meta_merged;
    sync 的幂等检查同时查留痕表 —— 物理删除过的文件不再被 _sync_group_files
    (全量扫描) 重新插入。曾致同一图片型 PDF 入库→删除→重插→反复 OCR。
用法: python -m scripts.merge_report_duplicates
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logger import get_logger
from storage.pg import PgClient
from core.helpers import is_chinese_translated

logger = get_logger(__name__)


def _ensure_merged_table() -> None:
    """留痕表 DDL: 独立连接立即提交 (DDL 不进业务事务, 防锁表)"""
    import psycopg2
    from core.config import get_config as _gc
    cfg = _gc().pg_config
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], dbname=cfg["database"], connect_timeout=10,
        options="-c search_path=stock,fin,public")
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin.report_meta_merged (
                file_id     BIGINT PRIMARY KEY,
                report_id   INTEGER,
                topic_id    BIGINT,
                file_name   VARCHAR(256),
                merged_at   TIMESTAMP DEFAULT now()
            )""")
        cur.close()
    finally:
        conn.close()


def _delete_row(pg: PgClient, r: dict) -> None:
    """删除条目: 带 file_id 的先落留痕表 (防 sync 全量重插), 再级联删预测"""
    if r.get("file_id"):
        pg.execute(
            """INSERT INTO fin.report_meta_merged (file_id, report_id, topic_id, file_name)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (file_id) DO NOTHING""",
            (r["file_id"], r["report_id"], r.get("topic_id"),
             (r.get("file_name") or "")[:256]))
    pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s", (r["report_id"],))
    pg.execute("DELETE FROM fin.report_meta WHERE report_id=%s", (r["report_id"],))


def _keep_key(r: dict) -> tuple:
    """保留优先级: PDF > 非中文翻译版 > 已分析 > 新记录 (升序, 小者保留)"""
    is_pdf = (r.get("source") == "zsxq" and (r.get("file_name") or "").lower().endswith(".pdf"))
    return (
        0 if is_pdf else 1,
        0 if not is_chinese_translated(r.get("file_name") or "") else 1,
        0 if r.get("analysis_status") == "done" else 1,
        r["report_id"],
    )


def merge() -> dict:
    _ensure_merged_table()
    with PgClient() as pg:
        # 1. 找出每个 topic 的全部条目
        topics = pg.fetch_all(
            """SELECT topic_id,
                      COUNT(*) AS n,
                      COUNT(*) FILTER (WHERE source = 'zsxq' AND file_name ILIKE '%.pdf') AS pdf_n
               FROM fin.report_meta
               WHERE topic_id IS NOT NULL
               GROUP BY topic_id
               HAVING COUNT(*) > 1""")

        merged = deleted = 0
        for t in topics:
            rows = pg.fetch_all(
                """SELECT report_id, topic_id, file_id, source, file_name,
                          content_text, content_chars, analysis_status
                   FROM fin.report_meta WHERE topic_id = %s""",
                (t["topic_id"],))
            if not rows:
                continue
            rows.sort(key=_keep_key)  # 保留对象排最前

            if t["pdf_n"] > 0:
                # 保留第一个 PDF 条目
                keep = rows[0]
                # 正文: PDF 自身文本为空时, 借同 topic 的 txt 解读文本
                keep_text = keep.get("content_text") or ""
                if not keep_text.strip():
                    donor = next((r for r in rows
                                  if (r.get("content_text") or "").strip()), None)
                    if donor:
                        pg.execute(
                            "UPDATE fin.report_meta SET content_text=%s, content_chars=%s, "
                            "extraction_status='extracted' WHERE report_id=%s",
                            (donor["content_text"], donor["content_chars"], keep["report_id"]))
                        merged += 1
                # 删除其余条目 (中文版/txt/mp3/重复pdf) — 留痕防重插
                for r in rows[1:]:
                    _delete_row(pg, r)
                    deleted += 1
            else:
                # 无 PDF: 保留 txt 条目但标记 (报表 API 过滤)
                keep = rows[0]
                if keep["source"] == "zsxq_topic":
                    pg.execute(
                        "UPDATE fin.report_meta SET source='zsxq_topic_pdf_absent' "
                        "WHERE report_id=%s", (keep["report_id"],))
                for r in rows[1:]:
                    _delete_row(pg, r)
                    deleted += 1

        # 2. 独立的 zsxq_topic 条目 (无同 topic 文件): 也标记为不展示
        pg.execute(
            """UPDATE fin.report_meta SET source='zsxq_topic_pdf_absent'
               WHERE source='zsxq_topic' AND topic_id NOT IN
                     (SELECT topic_id FROM fin.report_meta WHERE source='zsxq')""")
        # 3. mp3 等音频: 删除 (不是研报; 留痕防重插)
        mp3 = pg.fetch_all(
            "SELECT report_id, topic_id, file_id, file_name FROM fin.report_meta "
            "WHERE file_name ILIKE '%.mp3'")
        for r in mp3:
            _delete_row(pg, r)
            deleted += 1

        stat = pg.fetch_one(
            """SELECT source, COUNT(*) AS n FROM fin.report_meta GROUP BY source ORDER BY n DESC""")
        return {"topics_merged": merged, "deleted": deleted, "remaining": stat}


if __name__ == "__main__":
    print(merge())
