# -*- encoding: utf-8 -*-
"""Milvus collection 通用工具 (无业务知识)

业务 collection 的字段定义在各业务模块 (如 skills/report/collection.py),
本模块只提供通用件: 建 schema / 向量索引参数 / 幂等建 collection / 重建 / 查维度。
"""
from __future__ import annotations

import logging
from typing import Optional

from pymilvus import MilvusClient

logger = logging.getLogger(__name__)


def new_schema(auto_id: bool = False, enable_dynamic_field: bool = False):
    """新建空 schema (业务侧链式 add_field 填字段)"""
    return MilvusClient.create_schema(auto_id=auto_id, enable_dynamic_field=enable_dynamic_field)


def vector_index_params(client, field: str = "embedding", metric_type: str = "COSINE",
                        index_type: str = "HNSW", **params):
    """向量索引参数 (默认 HNSW + COSINE; M/efConstruction 等经 params 覆盖)"""
    index_params = client.prepare_index_params()
    kw = {"M": 16, "efConstruction": 200}
    kw.update(params)
    index_params.add_index(field_name=field, index_type=index_type,
                           metric_type=metric_type, params=kw)
    return index_params


def ensure_collection(client, name: str, schema, index_params) -> bool:
    """幂等建 collection (已存在跳过); 返回是否新建"""
    if client.has_collection(name):
        return False
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info(f"已创建 Milvus collection: {name}")
    return True


def rebuild_collection(client, name: str, schema, index_params) -> None:
    """drop + 重建 (换 embedding 模型/维度后用; 数据作废须重灌)"""
    if client.has_collection(name):
        client.drop_collection(name)
        logger.warning(f"已 drop collection: {name} (重建)")
    ensure_collection(client, name, schema, index_params)


def collection_vector_dim(client, name: str, field: str = "embedding") -> Optional[int]:
    """已建 collection 的向量维度 (不存在/无该字段返回 None)"""
    if not client.has_collection(name):
        return None
    for f in client.describe_collection(name).get("fields", []):
        if f.get("name") == field:
            return (f.get("params") or {}).get("dim")
    return None
