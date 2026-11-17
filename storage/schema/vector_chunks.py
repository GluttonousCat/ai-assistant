"""Physical schemas for the report and annual-report chunk indexes."""

from pymilvus import DataType
from storage.milvus_schema import new_schema

CHUNK_TEXT_MAX = 16384
COLLECTION_REPORT_CHUNKS = "report_chunks"
COLLECTION_ANNUAL_CHUNKS = "annual_chunks"


def report_schema(dim: int):
    """Field definitions (deterministic primary key "{report_id}:{chunk_idx}", upsert is idempotent)"""
    schema = new_schema()
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("report_id", DataType.INT64)  # -> fin.report_meta.report_id
    schema.add_field("chunk_idx", DataType.INT64)  # chunk ordinal (0-based)
    schema.add_field(
        "ts_code", DataType.VARCHAR, max_length=16
    )  # '' = non-A-share / unknown
    schema.add_field(
        "market", DataType.VARCHAR, max_length=8
    )  # vocabulary same as fin.report_meta.market
    schema.add_field(
        "org_name", DataType.VARCHAR, max_length=64
    )  # broker (echoed in retrieval)
    schema.add_field("publish_year", DataType.INT32)  # 0 = unknown (no DATE type)
    schema.add_field(
        "section", DataType.VARCHAR, max_length=256
    )  # title path (echo + filter)
    schema.add_field("chunk_type", DataType.VARCHAR, max_length=16)  # text/table/chart
    schema.add_field("chunk_text", DataType.VARCHAR, max_length=CHUNK_TEXT_MAX)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    return schema


def annual_schema(dim: int):
    schema = new_schema()
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("ts_code", DataType.VARCHAR, max_length=16)  # -> stock.stock_basic
    schema.add_field("report_year", DataType.INT32)  # report year
    schema.add_field(
        "category", DataType.VARCHAR, max_length=8
    )  # ndbg annual / bndbg semi-annual
    schema.add_field(
        "section_key", DataType.VARCHAR, max_length=32
    )  # mdna/governance/matters/shareholders
    schema.add_field("section_title", DataType.VARCHAR, max_length=128)
    schema.add_field("subsection_title", DataType.VARCHAR, max_length=256)
    schema.add_field("chunk_text", DataType.VARCHAR, max_length=CHUNK_TEXT_MAX)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    return schema
