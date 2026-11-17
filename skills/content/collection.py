"""Compatibility exports for the derived annual-report index.

Stable company/year/category/section/subsection keys permit idempotent reloads.
Exact KPI tables stay outside the vector index; revised chunks require cleanup.
"""

from tools.finance.vector_collections import (
    CHUNK_TEXT_MAX,
    COLLECTION_ANNUAL_CHUNKS,
    ensure_annual_chunks,
    rebuild_annual_chunks,
    delete_annual_chunks,
    annual_schema as _schema,
    annual_collection,
)

_ensure_created = annual_collection.cached_ensure

__all__ = [
    "CHUNK_TEXT_MAX",
    "COLLECTION_ANNUAL_CHUNKS",
    "ensure_annual_chunks",
    "rebuild_annual_chunks",
    "delete_annual_chunks",
    "_schema",
    "_ensure_created",
]
