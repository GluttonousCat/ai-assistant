# -*- encoding: utf-8 -*-
"""
研报去重合并: 知识星球同一研报常有多条记录 —
    source='zsxq' (话题附件: PDF 原研报, 可能正文为空)
    source='zsxq_topic' (话题文本: 中文解读 txt)
    source='zsxq_file' (群文件: 含 mp3 音频)
规则:
    1. 每个 topic 保留 PDF 条目 (正文优先 PDF 提取文本, 空则并入话题解读文本);
    2. 同 topic 的 zsxq_topic (txt) / zsxq_file(mp3) 条目删除 (内容已并入);
    3. 无 PDF 的 topic: 保留 txt 解读条目 (source 改 'zsxq_topic_pdf_absent',
       报表 API 仍不展示, 仅作溯源)。
用法: python -m scripts.merge_report_duplicates
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logger import get_logger
from storage.pg import PgClient

logger = get_logger(__name__)


def merge() -> dict:
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
                """SELECT report_id, source, file_name, content_text, content_chars
                   FROM fin.report_meta WHERE topic_id = %s
                   ORDER BY (source = 'zsxq' AND right(file_name, 4) = '.pdf') DESC, report_id""",
                (t["topic_id"],))
            if not rows:
                continue

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
                # 删除其余条目 (txt/mp3/重复pdf)
                for r in rows[1:]:
                    pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s",
                               (r["report_id"],))
                    pg.execute("DELETE FROM fin.report_meta WHERE report_id=%s",
                               (r["report_id"],))
                    deleted += 1
            else:
                # 无 PDF: 保留 txt 条目但标记 (报表 API 过滤)
                keep = rows[0]
                if keep["source"] == "zsxq_topic":
                    pg.execute(
                        "UPDATE fin.report_meta SET source='zsxq_topic_pdf_absent' "
                        "WHERE report_id=%s", (keep["report_id"],))
                for r in rows[1:]:
                    pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s",
                               (r["report_id"],))
                    pg.execute("DELETE FROM fin.report_meta WHERE report_id=%s",
                               (r["report_id"],))
                    deleted += 1

        # 2. 独立的 zsxq_topic 条目 (无同 topic 文件): 也标记为不展示
        pg.execute(
            """UPDATE fin.report_meta SET source='zsxq_topic_pdf_absent'
               WHERE source='zsxq_topic' AND topic_id NOT IN
                     (SELECT topic_id FROM fin.report_meta WHERE source='zsxq')""")
        # 3. mp3 等音频: 删除 (不是研报)
        mp3 = pg.fetch_all(
            "SELECT report_id FROM fin.report_meta WHERE file_name ILIKE '%.mp3'")
        for r in mp3:
            pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s",
                       (r["report_id"],))
            pg.execute("DELETE FROM fin.report_meta WHERE report_id=%s",
                       (r["report_id"],))
            deleted += 1

        stat = pg.fetch_one(
            """SELECT source, COUNT(*) AS n FROM fin.report_meta GROUP BY source ORDER BY n DESC""")
        return {"topics_merged": merged, "deleted": deleted, "remaining": stat}


if __name__ == "__main__":
    print(merge())
