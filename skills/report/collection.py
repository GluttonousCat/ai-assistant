# -*- encoding: utf-8 -*-
"""report_chunks collection — business definitions for report-chunk semantic retrieval (fields / lifecycle)

Contracts:
- PG is the master, Milvus the replica: report_id references fin.report_meta.report_id;
  deleting a report must also call delete_report_chunks(report_id) (best effort, must not block the main flow);
- dimension = milvus_config()["embed_dim"], strongly coupled with the embedding model;
  switching models requires rebuild_report_chunks() to recreate and re-ingest;
- scalars have no NULL: unknown ts_code uses '', unknown publish_year uses 0;
- chunking contract (ingestion must obey): cleaning strips headers/footers and the disclaimer tail;
  linearized table text never goes into embedding (numbers broken across lines are poison — the
  "[表格: …]" placeholder is used instead); after a re-chunking algorithm change, re-ingestion must
  first call delete_report_chunks to clear orphans (chunk_idx shifts as a whole).
"""
from __future__ import annotations

import logging
from functools import lru_cache

from pymilvus import DataType

from storage.milvus import delete_by_expr, get_milvus, milvus_config
from storage.milvus_schema import (
    collection_vector_dim,
    ensure_collection,
    new_schema,
    rebuild_collection,
    vector_index_params,
)

logger = logging.getLogger(__name__)

COLLECTION_REPORT_CHUNKS = "report_chunks"

CHUNK_TEXT_MAX = 16384


def _schema(dim: int):
    """Field definitions (deterministic primary key "{report_id}:{chunk_idx}", upsert is idempotent)"""
    schema = new_schema()
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("report_id", DataType.INT64)                 # -> fin.report_meta.report_id
    schema.add_field("chunk_idx", DataType.INT64)                 # chunk ordinal (0-based)
    schema.add_field("ts_code", DataType.VARCHAR, max_length=16)  # '' = non-A-share / unknown
    schema.add_field("market", DataType.VARCHAR, max_length=8)    # vocabulary same as fin.report_meta.market
    schema.add_field("org_name", DataType.VARCHAR, max_length=64)  # broker (echoed in retrieval)
    schema.add_field("publish_year", DataType.INT32)              # 0 = unknown (no DATE type)
    schema.add_field("section", DataType.VARCHAR, max_length=256)  # title path (echo + filter)
    schema.add_field("chunk_type", DataType.VARCHAR, max_length=16)  # text/table/chart
    schema.add_field("chunk_text", DataType.VARCHAR, max_length=CHUNK_TEXT_MAX)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    return schema


@lru_cache(maxsize=1)
def _ensure_created() -> bool:
    client = get_milvus()
    dim = milvus_config()["embed_dim"]
    cur_dim = collection_vector_dim(client, COLLECTION_REPORT_CHUNKS)
    if cur_dim is not None and cur_dim != dim:
        logger.warning(
            f"{COLLECTION_REPORT_CHUNKS} 维度 {cur_dim} != 配置 {dim} "
            f"(换过 embedding 模型?); 需 rebuild_report_chunks() 重建并重灌")
    return ensure_collection(client, COLLECTION_REPORT_CHUNKS,
                             _schema(dim), vector_index_params(client))


def ensure_report_chunks() -> bool:
    """Idempotently create the collection (once per process); returns whether it was newly created this time"""
    return _ensure_created()


def rebuild_report_chunks() -> None:
    """Drop + recreate (after switching embedding model/dimension; old vectors are void and must be re-ingested)"""
    client = get_milvus()
    dim = milvus_config()["embed_dim"]
    rebuild_collection(client, COLLECTION_REPORT_CHUNKS,
                       _schema(dim), vector_index_params(client))
    _ensure_created.cache_clear()


def delete_report_chunks(report_id: int) -> int:
    """Delete all vectors of a single report (chained from the PG-side delete).

    Best effort: if Milvus is unreachable, warn without blocking (the merge main
    flow must not break on vector-layer failure; orphan vectors are harmless —
    when retrieval echoes a report_id that PG no longer has, the upper layer discards it).
    """
    try:
        return delete_by_expr(COLLECTION_REPORT_CHUNKS,
                              f"report_id == {report_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清理 {COLLECTION_REPORT_CHUNKS} report_id={report_id} "
                       f"失败 (不阻断): {e}")
        return 0
