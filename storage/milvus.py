# -*- encoding: utf-8 -*-
from __future__ import annotations

import logging
import os
import random
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml
from pymilvus import DataType, MilvusClient

from storage.milvus_schema import (
    new_schema,
    rebuild_collection,
    vector_index_params,
)

logger = logging.getLogger(__name__)

_client: MilvusClient | None = None
_lock = threading.Lock()
_not_ready_warned = False


@lru_cache(maxsize=1)
def _yaml_milvus() -> Dict[str, Any]:
    """config.yaml 的 milvus 段 (进程内读一次; 文件缺失/无该段返回空 dict)"""
    path = Path(__file__).resolve().parent.parent / "config.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data.get("milvus"), dict):
            return data["milvus"]
    except Exception as e:
        logger.warning(f"config.yaml 读取失败将采用默认值: {e}")
    return {}


def milvus_config() -> Dict[str, Any]:
    """Milvus 连接配置: env MILVUS_* > config.yaml milvus 段 > 默认值 (env > yaml)"""
    def _pick(env_key: str, yaml_key: str, default: Any) -> Any:
        v = os.environ.get(env_key)
        if v not in (None, ""):
            return v
        return _yaml_milvus().get(yaml_key, default)

    return {
        "uri": str(_pick("MILVUS_URI", "uri", "http://127.0.0.1:19530")),
        "token": str(_pick("MILVUS_TOKEN", "token", "")),
        "db_name": str(_pick("MILVUS_DB", "db_name", "default")),
        "embed_dim": int(_pick("MILVUS_EMBED_DIM", "embed_dim", 1024)),
    }


def check_milvus_config(cfg: Dict[str, Any]) -> List[str]:
    """配置检测"""
    problems: List[str] = []
    uri = str(cfg.get("uri") or "")
    if not (uri.startswith("http://") or uri.startswith("https://")):
        problems.append(f"uri 非法: {uri!r} (须 http(s)://host:port)")
    try:
        dim = int(cfg.get("embed_dim", 0))
        if dim <= 0:
            problems.append(f"embed_dim 须为正整数: {dim}")
    except (TypeError, ValueError):
        problems.append(f"embed_dim 非数字: {cfg.get('embed_dim')!r}")
    return problems


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

def insert_rows(collection: str, rows: List[Dict[str, Any]]) -> int:
    """插入"""
    if not rows:
        return 0
    res = get_milvus().insert(collection_name=collection, data=rows)
    return int(res.get("insert_count", len(rows))) \
        if isinstance(res, dict) else len(rows)

def upsert_rows(collection: str, rows: List[Dict[str, Any]]) -> int:
    """按主键 upsert"""
    if not rows:
        return 0
    res = get_milvus().upsert(collection_name=collection, data=rows)
    return int(res.get("upsert_count", len(rows))) \
        if isinstance(res, dict) else len(rows)

def search_similar(
        collection: str, vector: Sequence[float], limit: int = 5,
        expr: Optional[str] = None, output_fields: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """ANN"""
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

def fetch_by_expr(
        collection: str, expr: str, output_fields: Optional[List[str]] = None,
        limit: int = 100
) -> List[Dict[str, Any]]:
    """标量过滤查询"""
    return list(get_milvus().query(
        collection_name=collection, filter=expr,
        output_fields=output_fields or ["*"], limit=limit))

def delete_by_expr(collection: str, expr: str) -> int:
    """按标量过滤删除"""
    res = get_milvus().delete(collection_name=collection, filter=expr)
    return int(res.get("delete_count", 0)) if isinstance(res, dict) else 0

def count_rows(collection: str) -> int:
    """行数"""
    stats = get_milvus().get_collection_stats(collection_name=collection)
    return int(stats.get("row_count", 0)) if isinstance(stats, dict) else 0

SMOKE_COLLECTION = "storage_smoke"

def _status() -> None:
    ready = milvus_ready()
    logger.info(f"Milvus 探活: {'OK' if ready else '不可达 (向量功能将降级)'}")
    if not ready:
        return
    client = get_milvus()
    cfg = milvus_config()
    logger.info(f"uri={cfg['uri']} db={cfg['db_name']} embed_dim={cfg['embed_dim']}")
    for name in sorted(client.list_collections()):
        logger.info(f"{name}: 行数={count_rows(name)}")

def _smoke() -> int | None:
    """SDK 自检: 临时 collection 写入 -> 检索 -> 清理 (不碰业务数据)"""
    if not milvus_ready():
        logger.warning("Milvus 不可达, 冒烟终止 (exit 1)")
        return 1
    dim = milvus_config()["embed_dim"]
    schema = new_schema()
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=32)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    client = get_milvus()
    rebuild_collection(client, SMOKE_COLLECTION, schema, vector_index_params(client))
    try:
        vec = [random.random() for _ in range(dim)]
        n = upsert_rows(SMOKE_COLLECTION, [{"id": "smoke", "embedding": vec}])
        hits = search_similar(SMOKE_COLLECTION, vec, limit=1)
        top = f"top1 distance={hits[0]['distance']:.4f}" if hits else "未命中"
        logger.info(f"写入 {n} 行, 检索 {len(hits)} ({top}) —— SDK 冒烟通过")
        return 0
    finally:
        client.drop_collection(SMOKE_COLLECTION)
