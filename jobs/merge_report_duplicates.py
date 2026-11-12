# -*- encoding: utf-8 -*-
"""
Research-report dedup merge: the same Zhixingqiu report often has multiple records —
    source='zsxq' (topic attachment: original PDF report, body text may be empty)
    source='zsxq_topic' (topic text: Chinese commentary txt)
    source='zsxq_file' (group file: includes mp3 audio)
Rules:
    1. Keep one PDF entry per topic (body text prefers the PDF-extracted text; if
       empty, merge in the topic commentary text);
       keep priority: non-Chinese-translated > analyzed (done) > newer record —
       deletions concentrate on Chinese translations and un-analyzed entries (when a
       Chinese version pairs with the original, only the original is kept);
    2. Delete the same topic's zsxq_topic (txt) / zsxq_file (mp3) entries (content
       already merged);
    3. Topics without a PDF: keep the txt commentary entry (source becomes
       'zsxq_topic_pdf_absent'; the reports API still hides it, kept only for
       traceability).
Audit trail (anti ping-pong loop, 2026-09-03): before deleting an entry with a
file_id, write it to fin.report_meta_merged; the sync's idempotent check also
consults the audit table — physically deleted files are no longer re-inserted by
_sync_group_files (full scan). This once caused the same image-only PDF to be
ingested → deleted → re-inserted → OCR'd repeatedly.
Usage: python -m scripts.merge_report_duplicates
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import DDL_REPORT_MERGED, ddl_cursor
from core.helpers import is_chinese_translated

logger = get_logger(__name__)


def _ensure_merged_table() -> None:
    """Audit-table DDL: independent connection, immediate commit (DDL must not run
    inside a business transaction — prevents table locks);
    single source of truth for DDL lives in storage.pg_schema (same source as
    init_report_schema)."""
    with ddl_cursor() as cur:
        cur.execute(DDL_REPORT_MERGED)


def _delete_row(pg: PgClient, r: dict) -> None:
    """Delete an entry: entries with a file_id go to the audit table first (prevents
    full-scan re-insertion by sync), then forecasts are deleted in cascade."""
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
    """Keep priority: PDF > non-Chinese-translated > analyzed > newer record
    (ascending; smallest sorts first and is kept)."""
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
        # 1. Find all entries of each topic
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
            rows.sort(key=_keep_key)  # the entry to keep sorts first

            if t["pdf_n"] > 0:
                # Keep the first PDF entry
                keep = rows[0]
                # Body text: when the PDF's own text is empty, borrow the topic's
                # txt commentary text
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
                # Delete the remaining entries (Chinese versions/txt/mp3/duplicate
                # pdf) — audit trail prevents re-insertion
                for r in rows[1:]:
                    _delete_row(pg, r)
                    deleted += 1
            else:
                # No PDF: keep the txt entry but mark it (filtered out by the reports API)
                keep = rows[0]
                if keep["source"] == "zsxq_topic":
                    pg.execute(
                        "UPDATE fin.report_meta SET source='zsxq_topic_pdf_absent' "
                        "WHERE report_id=%s", (keep["report_id"],))
                for r in rows[1:]:
                    _delete_row(pg, r)
                    deleted += 1

        # 2. Standalone zsxq_topic entries (no files on the same topic): also mark
        #    as not displayed
        pg.execute(
            """UPDATE fin.report_meta SET source='zsxq_topic_pdf_absent'
               WHERE source='zsxq_topic' AND topic_id NOT IN
                     (SELECT topic_id FROM fin.report_meta WHERE source='zsxq')""")
        # 3. mp3 and other audio: delete (not research reports; audit trail
        #    prevents re-insertion)
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
