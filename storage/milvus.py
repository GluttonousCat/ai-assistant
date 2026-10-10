# -*- encoding: utf-8 -*-
from __future__ import annotations

import logging
import random
import threading
from typing import Any, Dict, List, Optional, Sequence

from pymilvus import MilvusClient

from storage.milvus_schema import (
    COLLECTION_ENSURES,
    COLLECTION_REPORT_CHUNKS,
    check_milvus_config,
    milvus_config,
)

logger = logging.getLogger(__name__)

_client: MilvusClient | None = None
_lock = threading.Lock()
_not_ready_warned = False
_ensured: set = set()


def get_milvus():
    """取 Milvus 单例实例"""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        cfg = milvus_config()
        for problem in check_milvus_config(cfg):
            logger.warning(f"Milvus 配置异常: {problem}")
        kwargs: Dict[str, Any] = {"uri": cfg["uri"], "db_name": cfg["db_name"]}
        if cfg["token"]:
            kwargs["token"] = cfg["token"]
        try:
            _client = MilvusClient(**kwargs)
            _client.list_collections()
        except Exception as e:
            _client = None
            raise RuntimeError(
                f"Milvus 不可达 ({cfg['uri']}): {e} —— "
                f"检查服务端是否已启动/.env MILVUS_URI.") from e
        logger.info(f"Milvus 已连接: {cfg['uri']}/{cfg['db_name']}")
        return _client

def close_milvus() -> None:
    """关闭连接"""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception as e:
                logger.warning(f"关闭 Milvus 连接失败: {e}")
            _client = None

def milvus_ready() -> bool:
    """探活"""
    global _not_ready_warned
    try:
        get_milvus()
        return True
    except RuntimeError as e:
        if not _not_ready_warned:
            logger.warning(f"向量检索不可用, 相关功能降级: {e}")
            _not_ready_warned = True
        return False


def _ensure(collection: str) -> None:
    """按注册表惰性建 collection"""
    if collection in _ensured:
        return
    ensure_fn = COLLECTION_ENSURES.get(collection)
    if ensure_fn is not None:
        ensure_fn(get_milvus())
    _ensured.add(collection)

def insert_rows(collection: str, rows: List[Dict[str, Any]]) -> int:
    """插入"""
    if not rows:
        return 0
    _ensure(collection)
    res = get_milvus().insert(collection_name=collection, data=rows)
    return int(res.get("insert_count", len(rows))) \
        if isinstance(res, dict) else len(rows)

def upsert_rows(collection: str, rows: List[Dict[str, Any]]) -> int:
    """按主键 upsert"""
    if not rows:
        return 0
    _ensure(collection)
    res = get_milvus().upsert(collection_name=collection, data=rows)
    return int(res.get("upsert_count", len(rows))) \
        if isinstance(res, dict) else len(rows)

def search_similar(collection: str, vector: Sequence[float], limit: int = 5,
                   expr: Optional[str] = None,
                   output_fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """向量近邻检索"""
    _ensure(collection)
    res = get_milvus().search(
        collection_name=collection,
        data=[list(vector)],
        limit=limit,
        filter=expr or "",
        output_fields=output_fields or ["*"],
    )
    rows: List[Dict[str, Any]] = []
    for hit in (res[0] if res else []):
        row = {"id": hit.get("id"), "distance": hit.get("distance")}
        row.update(hit.get("entity") or {})
        rows.append(row)
    return rows

def fetch_by_expr(collection: str, expr: str,
                  output_fields: Optional[List[str]] = None,
                  limit: int = 100) -> List[Dict[str, Any]]:
    """标量过滤查询"""
    _ensure(collection)
    return list(get_milvus().query(
        collection_name=collection, filter=expr,
        output_fields=output_fields or ["*"], limit=limit))

def delete_by_expr(collection: str, expr: str) -> int:
    """按标量过滤删除"""
    _ensure(collection)
    res = get_milvus().delete(collection_name=collection, filter=expr)
    return int(res.get("delete_count", 0)) if isinstance(res, dict) else 0

def count_rows(collection: str) -> int:
    """行数"""
    _ensure(collection)
    stats = get_milvus().get_collection_stats(collection_name=collection)
    return int(stats.get("row_count", 0)) if isinstance(stats, dict) else 0

def _status() -> None:
    ready = milvus_ready()
    logger.info(f"Milvus 探活: {'OK' if ready else '不可达 (向量功能将降级)'}")
    if not ready:
        return
    client = get_milvus()
    cfg = milvus_config()
    logger.info(f"uri={cfg['uri']} db={cfg['db_name']} embed_dim={cfg['embed_dim']}")
    for name in sorted(COLLECTION_ENSURES):
        exists = client.has_collection(name)
        n = count_rows(name) if exists else "-"
        logger.info(f"{name}: {'存在' if exists else '未建'}, 行数={n}")

def _smoke() -> int:
    """假向量全链路: 写入 -> 检索 -> 清理 (服务端须已启动)"""
    if not milvus_ready():
        logger.warning("Milvus 不可达, 冒烟终止 (exit 1)")
        return 1
    dim = milvus_config()["embed_dim"]
    chunk_id = "smoke:0"
    row = {
        "chunk_id": chunk_id, "report_id": 0, "chunk_idx": 0,
        "ts_code": "", "market": "其他", "org_name": "smoke",
        "publish_year": 2026, "chunk_text": "milvus 冒烟测试行",
        "embedding": [random.random() for _ in range(dim)],
    }
    try:
        n = upsert_rows(COLLECTION_REPORT_CHUNKS, [row])
        hits = search_similar(COLLECTION_REPORT_CHUNKS, row["embedding"],
                              limit=3, expr='org_name == "smoke"')
        logger.info(f"写入 {n} 行, 检索命中 {len(hits)} (top1 distance={hits[0]['distance']:.4f})"
                    if hits else "检索未命中")
        deleted = delete_by_expr(COLLECTION_REPORT_CHUNKS, f'chunk_id == "{chunk_id}"')
        logger.info(f"清理 {deleted} 行 —— 冒烟通过")
        return 0
    except Exception as e:
        logger.warning(f"冒烟失败: {e}")
        return 1
