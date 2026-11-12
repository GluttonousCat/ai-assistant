# -*- encoding: utf-8 -*-
"""annual_chunks collection — business definition of semantic retrieval over
annual/semi-annual report subsections

Data source: file artifacts of skills/content/annual_items.py —
output/cninfo/md/<code>/<year>_<category>_items.json sections.*.subsections
(one subsection = one chunk; the section-level text is empty, all prose lives
at the subsection level — verified on Moutai).

Contract:
- chunk_id = "{ts_code}:{year}:{category}:{section_key}:{sub_idx}" (deterministic
  primary key, idempotent reloads); re-ingesting a revised annual report must
  first delete orphans by prefix via delete_annual_chunks;
- items (kpi3y/nonrecurring) and tables never go into the vector store: exact
  numbers are table-lookup needs, served from PG/files;
- embedding text = "section title > subsection title\\nbody" (title semantics
  are key to recall);
- semi-annual and annual reports share the structure and both get ingested;
  retrieval defaults to category=ndbg first;
- the authoritative source is the file (items.json); this collection is a
  derived index.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

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

COLLECTION_ANNUAL_CHUNKS = "annual_chunks"

CHUNK_TEXT_MAX = 16384


def _schema(dim: int):
    schema = new_schema()
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("ts_code", DataType.VARCHAR, max_length=16)    # -> stock.stock_basic
    schema.add_field("report_year", DataType.INT32)                 # report year
    schema.add_field("category", DataType.VARCHAR, max_length=8)    # ndbg annual / bndbg semi-annual
    schema.add_field("section_key", DataType.VARCHAR, max_length=32)   # mdna/governance/matters/shareholders
    schema.add_field("section_title", DataType.VARCHAR, max_length=128)
    schema.add_field("subsection_title", DataType.VARCHAR, max_length=256)
    schema.add_field("chunk_text", DataType.VARCHAR, max_length=CHUNK_TEXT_MAX)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    return schema


@lru_cache(maxsize=1)
def _ensure_created() -> bool:
    client = get_milvus()
    dim = milvus_config()["embed_dim"]
    cur_dim = collection_vector_dim(client, COLLECTION_ANNUAL_CHUNKS)
    if cur_dim is not None and cur_dim != dim:
        logger.warning(
            f"{COLLECTION_ANNUAL_CHUNKS} 维度 {cur_dim} != 配置 {dim} "
            f"(换过 embedding 模型?); 需 rebuild_annual_chunks() 重建并重灌")
    return ensure_collection(client, COLLECTION_ANNUAL_CHUNKS,
                             _schema(dim), vector_index_params(client))


def ensure_annual_chunks() -> bool:
    """Idempotently create the collection (once per process); returns whether
    it was newly created"""
    return _ensure_created()


def rebuild_annual_chunks() -> None:
    """Drop + rebuild (after switching embedding model/dimension; old vectors
    are void and must be re-ingested)"""
    client = get_milvus()
    dim = milvus_config()["embed_dim"]
    rebuild_collection(client, COLLECTION_ANNUAL_CHUNKS,
                       _schema(dim), vector_index_params(client))
    _ensure_created.cache_clear()


def delete_annual_chunks(ts_code: str, report_year: int,
                         category: Optional[str] = None) -> int:
    """Clear vectors by company+year(+category) prefix (orphan prevention on
    revised re-ingestion).

    Best effort: if Milvus is unreachable, warn but do not block (the file is
    the authoritative source; vectors can be re-ingested).
    """
    expr = f'ts_code == "{ts_code}" and report_year == {report_year}'
    if category:
        expr += f' and category == "{category}"'
    try:
        return delete_by_expr(COLLECTION_ANNUAL_CHUNKS, expr)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清理 {COLLECTION_ANNUAL_CHUNKS} 失败 (不阻断, 可重灌): {e}")
        return 0
