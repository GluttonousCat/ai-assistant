# -*- encoding: utf-8 -*-
"""annual_chunks collection — 年报/半年报小节语义检索的业务定义

数据源: skills/content/annual_items.py 的文件产物
output/cninfo/md/<代码>/<年度>_<类别>_items.json 的 sections.*.subsections
(一个小节 = 一个 chunk; 节层 text 为空, 文字全在小节层 — 茅台实测)。

契约:
- chunk_id = "{ts_code}:{year}:{category}:{section_key}:{sub_idx}" (确定性主键,
  重灌幂等); 年报修订重灌须先按前缀 delete_annual_chunks 清孤儿;
- items(kpi3y/nonrecurring) 与 tables 不入向量: 精确数值是查表需求, 走 PG/文件;
- 嵌入文本 = "节标题 > 小节标题\\n正文" (标题语义对召回关键);
- 半年报与年报同构同灌, 检索默认 category=ndbg 优先;
- 权威源是文件 (items.json), 本 collection 是派生索引。
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
    schema.add_field("report_year", DataType.INT32)                 # 报告年度
    schema.add_field("category", DataType.VARCHAR, max_length=8)    # ndbg年报/bndbg半年报
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
    """幂等建 collection (进程内一次); 返回是否本次新建"""
    return _ensure_created()


def rebuild_annual_chunks() -> None:
    """drop + 重建 (换 embedding 模型/维度后; 原向量作废须重灌)"""
    client = get_milvus()
    dim = milvus_config()["embed_dim"]
    rebuild_collection(client, COLLECTION_ANNUAL_CHUNKS,
                       _schema(dim), vector_index_params(client))
    _ensure_created.cache_clear()


def delete_annual_chunks(ts_code: str, report_year: int,
                         category: Optional[str] = None) -> int:
    """按 公司+年度(+类别) 前缀清向量 (修订重灌防孤儿)。

    尽力而为: Milvus 不可达时告警不阻断 (权威源是文件, 向量可重灌)。
    """
    expr = f'ts_code == "{ts_code}" and report_year == {report_year}'
    if category:
        expr += f' and category == "{category}"'
    try:
        return delete_by_expr(COLLECTION_ANNUAL_CHUNKS, expr)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清理 {COLLECTION_ANNUAL_CHUNKS} 失败 (不阻断, 可重灌): {e}")
        return 0
