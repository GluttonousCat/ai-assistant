"""Shared lifecycle for derived vector indexes; no domain orchestration."""

from functools import lru_cache
import threading

from core.logger import get_logger
from storage.milvus import delete_by_expr, get_milvus, milvus_config
from storage.milvus_schema import (
    collection_vector_dim,
    ensure_collection,
    rebuild_collection,
    vector_index_params,
)

logger = get_logger(__name__)


class ChunkCollection:
    def __init__(self, name, schema):
        self.name = name
        self.schema = schema
        self._lock = threading.Lock()
        self.cached_ensure = lru_cache(maxsize=1)(self._create)

    def _create(self):
        client = get_milvus()
        dimension = milvus_config()["embed_dim"]
        current = collection_vector_dim(client, self.name)
        if current is not None and current != dimension:
            logger.warning(
                "%s维度%s与配置%s不符，须重建并重新入库", self.name, current, dimension
            )
        return ensure_collection(
            client, self.name, self.schema(dimension), vector_index_params(client)
        )

    def ensure(self):
        # functools' cache alone permits concurrent duplicate first calls.
        with self._lock:
            return self.cached_ensure()

    def rebuild(self):
        with self._lock:
            client = get_milvus()
            dimension = milvus_config()["embed_dim"]
            rebuild_collection(
                client, self.name, self.schema(dimension), vector_index_params(client)
            )
            self.cached_ensure.cache_clear()

    def delete(self, expression):
        try:
            return delete_by_expr(self.name, expression)
        except Exception as error:
            logger.warning("清理%s失败，可重新入库修复: %s", self.name, error)
            return 0
