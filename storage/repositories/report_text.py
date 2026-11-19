"""Report body and OCR state persistence shared by extraction and translation."""

from storage.pg import PgClient
from storage.schema.reports import (
    ensure_report_analysis_columns as ensure_analysis_schema,
)


def mark_ocr_pending(report_id):
    with PgClient() as pg:
        return pg.execute(
            "UPDATE fin.report_meta SET extraction_status='pending_ocr' "
            "WHERE report_id=%s AND extraction_status IS DISTINCT FROM 'pending_ocr'",
            (report_id,),
        )


def save_ocr_text(report_id, text):
    with PgClient() as pg:
        return pg.execute(
            "UPDATE fin.report_meta SET content_text=%s, content_chars=%s, "
            "extraction_status='extracted' WHERE report_id=%s",
            (text, len(text), report_id),
        )


def mark_ocr_failed(report_id):
    with PgClient() as pg:
        return pg.execute(
            "UPDATE fin.report_meta SET extraction_status='failed', "
            "analysis_status=CASE WHEN analysis_status='done' THEN 'done' ELSE 'failed' END "
            "WHERE report_id=%s AND extraction_status='pending_ocr'",
            (report_id,),
        )


def load_translation(report_id):
    with PgClient(read_only=True) as pg:
        return pg.fetch_one(
            "SELECT report_id, content_text, content_text_cn FROM fin.report_meta WHERE report_id=%s",
            (report_id,),
        )


def save_translation(report_id, text, source_text):
    with PgClient() as pg:
        return pg.execute(
            "UPDATE fin.report_meta SET content_text_cn=%s WHERE report_id=%s AND content_text_cn IS NULL "
            "AND content_text IS NOT DISTINCT FROM %s",
            (text, report_id, source_text),
        )


def translation_candidates():
    with PgClient(read_only=True) as pg:
        return pg.fetch_all(
            "SELECT report_id, left(content_text, 2000) head FROM fin.report_meta "
            "WHERE content_chars > 200 AND content_text_cn IS NULL "
            "AND COALESCE(source,'') NOT IN ('zsxq_topic','zsxq_topic_pdf_absent')"
        )


def scanned_metadata(report_id):
    with PgClient(read_only=True) as pg:
        return pg.fetch_one(
            "SELECT report_id, title, file_path, file_name, content_chars FROM fin.report_meta WHERE report_id=%s",
            (report_id,),
        )


def report_body(report_id):
    with PgClient(read_only=True) as pg:
        return pg.fetch_one(
            "SELECT title, content_text FROM fin.report_meta WHERE report_id=%s",
            (report_id,),
        )
