# -*- encoding: utf-8 -*-

from __future__ import annotations

import logging
from typing import Optional

from pymilvus import MilvusClient

logger = logging.getLogger(__name__)


def new_schema(auto_id: bool = False, enable_dynamic_field: bool = False):
    return MilvusClient.create_schema(
        auto_id=auto_id, enable_dynamic_field=enable_dynamic_field
    )

def vector_index_params(
        client, field: str = "embedding", metric_type: str = "COSINE",
        index_type: str = "HNSW", **params):
    """Vector index params"""
    index_params = client.prepare_index_params()
    kw = {"M": 16, "efConstruction": 200}
    kw.update(params)
    index_params.add_index(
        field_name=field, index_type=index_type, metric_type=metric_type,
        params=kw
    )
    return index_params

def ensure_collection(client, name: str, schema, index_params) -> bool:
    if client.has_collection(name):
        return False
    client.create_collection(
        collection_name=name, schema=schema, index_params=index_params)
    logger.info(f"已创建 Milvus collection: {name}")
    return True

def rebuild_collection(client, name: str, schema, index_params) -> None:
    """drop"""
    if client.has_collection(name):
        client.drop_collection(name)
        logger.warning(f"已 drop collection: {name} (重建)")
    ensure_collection(client, name, schema, index_params)

def collection_vector_dim(
        client, name: str, field: str = "embedding"
) -> Optional[int]:
    if not client.has_collection(name):
        return None
    for f in client.describe_collection(name).get("fields", []):
        if f.get("name") == field:
            return (f.get("params") or {}).get("dim")
    return None
