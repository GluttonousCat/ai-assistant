"""Compatibility exports for the derived report index.

PG remains authoritative; stable report_id:chunk_idx keys prevent duplicates.
Changing embeddings requires rebuilding and re-ingesting the index.
"""

from tools.finance.vector_collections import (
    CHUNK_TEXT_MAX,
    COLLECTION_REPORT_CHUNKS,
    ensure_report_chunks,
    rebuild_report_chunks,
    delete_report_chunks,
    report_schema as _schema,
    report_collection,
)

_ensure_created = report_collection.cached_ensure

__all__ = [
    "CHUNK_TEXT_MAX",
    "COLLECTION_REPORT_CHUNKS",
    "ensure_report_chunks",
    "rebuild_report_chunks",
    "delete_report_chunks",
    "_schema",
    "_ensure_created",
]
