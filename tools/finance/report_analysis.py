"""Report analysis persistence boundary; callers never hold PG transactions."""

from storage.repositories.report_analysis import (
    extraction_candidates,
    load_report,
    mark_failed,
    save_extraction,
    matched_reports,
)

__all__ = [
    "extraction_candidates",
    "load_report",
    "mark_failed",
    "save_extraction",
    "matched_reports",
]
