"""Operations for the derived report and annual-report vector indexes."""

from storage.repositories.vector_chunks import ChunkCollection
from storage.schema.vector_chunks import (
    CHUNK_TEXT_MAX,
    COLLECTION_ANNUAL_CHUNKS,
    COLLECTION_REPORT_CHUNKS,
    annual_schema,
    report_schema,
)

report_collection = ChunkCollection(COLLECTION_REPORT_CHUNKS, report_schema)
annual_collection = ChunkCollection(COLLECTION_ANNUAL_CHUNKS, annual_schema)


def ensure_report_chunks() -> bool:
    return report_collection.ensure()


def ensure_annual_chunks() -> bool:
    return annual_collection.ensure()


def rebuild_report_chunks() -> None:
    report_collection.rebuild()


def rebuild_annual_chunks() -> None:
    annual_collection.rebuild()


def delete_report_chunks(report_id: int) -> int:
    return report_collection.delete(f"report_id == {report_id}")


def delete_annual_chunks(ts_code: str, report_year: int, category=None) -> int:
    expression = f'ts_code == "{ts_code}" and report_year == {report_year}'
    if category:
        expression += f' and category == "{category}"'
    return annual_collection.delete(expression)


__all__ = [
    "CHUNK_TEXT_MAX",
    "COLLECTION_ANNUAL_CHUNKS",
    "COLLECTION_REPORT_CHUNKS",
    "annual_schema",
    "report_schema",
    "report_collection",
    "annual_collection",
    "ensure_report_chunks",
    "ensure_annual_chunks",
    "rebuild_report_chunks",
    "rebuild_annual_chunks",
    "delete_report_chunks",
    "delete_annual_chunks",
]
