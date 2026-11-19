"""File-ID based ingestion of downloaded report attachments and late-arriving originals."""

from storage.pg import PgClient


def attachment_states(file_ids):
    """Fetch existing rows and dedup tombstones together for a batch of source files."""
    if not file_ids:
        return {}
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            "SELECT file_id, file_path, extraction_status FROM fin.report_meta WHERE file_id=ANY(%s) "
            "UNION ALL SELECT file_id, NULL, 'merged' FROM fin.report_meta_merged WHERE file_id=ANY(%s)",
            (file_ids, file_ids),
        )
    return {row["file_id"]: row for row in rows}


def save_attachment(row, source, file_path, text, content_chars, status, *, previous_path=None):
    """Recheck identity under a per-file lock. Return a ready report ID for inline analysis."""
    file_id = row["file_id"]
    with PgClient() as pg:
        pg.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (file_id,))
        if pg.fetch_one("SELECT 1 FROM fin.report_meta_merged WHERE file_id=%s", (file_id,)):
            return None
        existing = pg.fetch_one(
            "SELECT report_id, file_path, extraction_status FROM fin.report_meta "
            "WHERE file_id=%s ORDER BY report_id LIMIT 1 FOR UPDATE", (file_id,),
        )
        if existing:
            if not file_path or existing["file_path"] == file_path:
                return None
            if existing["file_path"] != previous_path:
                return None
            # Old placeholders may carry topic commentary. The downloaded file
            # supplies a fresh body and requires a fresh translation and analysis.
            pg.execute(
                "UPDATE fin.report_meta SET file_path=%s, content_text=%s, content_chars=%s, "
                "extraction_status=%s, content_text_cn=NULL, analysis_json=NULL, analysis_status='none', "
                "tags_industries=NULL, tags_regions=NULL, tags_commodities=NULL, tags_themes=NULL, "
                "tags_targets=NULL, ts_code=NULL, symbols=NULL WHERE report_id=%s",
                (file_path, text, content_chars, status, existing["report_id"]),
            )
            pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s", (existing["report_id"],))
            return existing["report_id"]
        result = pg.fetch_one(
            "INSERT INTO fin.report_meta (topic_id, file_id, title, file_name, file_path, file_size, "
            "content_text, content_chars, source, extraction_status, publish_date) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING report_id",
            (row.get("topic_id"), file_id, row["name"][:250], row["name"], file_path, row.get("size"),
             text, content_chars, source, status, (row.get("create_time") or "")[:10] or None),
        )
    return result["report_id"] if file_path else None
