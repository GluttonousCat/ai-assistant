"""Merge duplicate attachment records without confusing topics with documents."""

import re
import unicodedata

from core.helpers import is_chinese_translated
from storage.pg import PgClient
from storage.schema.connection import ddl_cursor
from storage.schema.reports import DDL_REPORT_MERGED


def _original_name(name):
    name = unicodedata.normalize("NFKC", name.strip()).casefold()
    name = re.sub(r"^[【(\[]?中文版[】)\]]?[\s_—·-]*", "", name)
    return re.sub(r"[-_—·]中文版(?=[-_·.]|$)", "", name)


def _duplicate_rows(rows):
    """Prefer analyzed records of the same file. Pair only unambiguous translations."""
    files, duplicates, kept = set(), [], []
    ordered = sorted(rows, key=lambda r: (
        is_chinese_translated(r.get("file_name") or ""),
        r["analysis_status"] != "done", r["report_id"],
    ))
    for row in ordered:
        fid = row.get("file_id")
        if fid is not None and fid in files:
            duplicates.append(row)
        else:
            files.add(fid)
            kept.append(row)
    originals = {}
    for row in kept:
        name = row.get("file_name") or ""
        if name and not is_chinese_translated(name):
            originals.setdefault(_original_name(name), []).append(row)
    for row in kept:
        name = row.get("file_name") or ""
        if is_chinese_translated(name) and len(originals.get(_original_name(name), [])) == 1:
            duplicates.append(row)
    return duplicates


def _delete_row(pg, row):
    if row.get("file_id") is not None:
        pg.execute(
            "INSERT INTO fin.report_meta_merged (file_id, report_id, topic_id, file_name) "
            "SELECT %s,%s,%s,%s WHERE NOT EXISTS (SELECT 1 FROM fin.report_meta "
            "WHERE file_id=%s AND report_id<>%s) ON CONFLICT (file_id) DO NOTHING",
            (row["file_id"], row["report_id"], row["topic_id"], row["file_name"],
             row["file_id"], row["report_id"]),
        )
    pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s", (row["report_id"],))
    pg.execute("DELETE FROM fin.report_meta WHERE report_id=%s", (row["report_id"],))


def merge():
    with ddl_cursor() as cur:
        cur.execute(DDL_REPORT_MERGED)
    # A topic may contain independent PDFs or Word documents and a separate
    # commentary. Only attachment identity or a paired translation proves duplication.
    attachments = (
        "source IN ('zsxq','zsxq_file') AND "
        "file_name ~* '\\.(pdf|docx?)$'"
    )
    deleted = 0
    with PgClient() as pg:
        topics = pg.fetch_all(
            f"SELECT topic_id FROM fin.report_meta WHERE topic_id IS NOT NULL AND {attachments} "
            "GROUP BY topic_id HAVING COUNT(file_id) > COUNT(DISTINCT file_id) "
            "OR (COUNT(*) > 1 AND BOOL_OR(file_name LIKE '%中文版%')) ORDER BY topic_id"
        )
        for topic in topics:
            rows = pg.fetch_all(
                "SELECT report_id, file_id, topic_id, file_name, analysis_status FROM fin.report_meta "
                f"WHERE topic_id=%s AND {attachments} ORDER BY report_id FOR UPDATE",
                (topic["topic_id"],),
            )
            for row in _duplicate_rows(rows):
                _delete_row(pg, row)
                deleted += 1

        pg.execute(
            "UPDATE fin.report_meta topic SET source='zsxq_topic_pdf_absent' "
            "WHERE topic.source='zsxq_topic' AND NOT EXISTS "
            f"(SELECT 1 FROM fin.report_meta WHERE topic_id=topic.topic_id AND {attachments})"
        )
        pg.execute(
            "UPDATE fin.report_meta topic SET source='zsxq_topic' "
            "WHERE topic.source='zsxq_topic_pdf_absent' AND EXISTS "
            f"(SELECT 1 FROM fin.report_meta WHERE topic_id=topic.topic_id AND {attachments})"
        )
        audio = pg.fetch_all(
            "SELECT report_id, file_id, topic_id, file_name FROM fin.report_meta "
            "WHERE source IN ('zsxq','zsxq_file') AND file_name ILIKE '%.mp3' ORDER BY report_id FOR UPDATE"
        )
        for row in audio:
            _delete_row(pg, row)
            deleted += 1
        stat = pg.fetch_one("SELECT source, COUNT(*) AS n FROM fin.report_meta GROUP BY source ORDER BY n DESC")
    return {"topics_merged": 0, "deleted": deleted, "remaining": stat}
