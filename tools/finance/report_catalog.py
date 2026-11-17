"""Provider-free report catalog operations for HTTP and other adapters."""

from storage.repositories.reports import (
    list_reports,
    report_tags,
    report_stocks,
    report_detail,
    report_file,
    legacy_reports,
    legacy_detail,
    add_report,
)

__all__ = [
    "list_reports",
    "report_tags",
    "report_stocks",
    "report_detail",
    "report_file",
    "legacy_reports",
    "legacy_detail",
    "add_report",
]
