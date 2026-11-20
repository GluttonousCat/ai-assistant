"""Report analysis persistence boundary; callers never hold PG transactions."""

from storage.repositories.report_analysis import (
    completed_attachment_page,
    extraction_candidates,
    load_report,
    mark_failed,
    save_extraction,
    matched_reports,
    reports_ingested_between,
)

__all__ = [
    "completed_attachment_page",
    "extraction_candidates",
    "load_report",
    "mark_failed",
    "save_extraction",
    "matched_reports",
    "reports_ingested_between",
]
