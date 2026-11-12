# -*- encoding: utf-8 -*-
from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml
from pymilvus import MilvusClient

logger = logging.getLogger(__name__)

_client: MilvusClient | None = None
_lock = threading.Lock()
_not_ready_warned = False


@lru_cache(maxsize=1)
def _yaml_milvus() -> Dict[str, Any]:
    """config.yaml milvus section (read once per process; missing file/section yields an empty dict)"""
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
    """Milvus connection config: env MILVUS_* > config.yaml milvus section > defaults (env > yaml)"""
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
    """Config validation"""
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
    """Get the singleton Milvus client"""
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
    """Close the connection"""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception as e:
                logger.warning(f"关闭 Milvus 连接失败: {e}")
            _client = None

def milvus_ready() -> bool:
    """Liveness probe"""
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
    """Insert rows"""
    if not rows:
        return 0
    res = get_milvus().insert(collection_name=collection, data=rows)
    return int(res.get("insert_count", len(rows))) \
        if isinstance(res, dict) else len(rows)

def upsert_rows(collection: str, rows: List[Dict[str, Any]]) -> int:
    """Upsert by primary key"""
    if not rows:
        return 0
    res = get_milvus().upsert(collection_name=collection, data=rows)
    return int(res.get("upsert_count", len(rows))) \
        if isinstance(res, dict) else len(rows)

def search_similar(
        collection: str, vector: Sequence[float], limit: int = 5,
        expr: Optional[str] = None, output_fields: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """ANN search"""
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
    """Scalar-filtered query"""
    return list(get_milvus().query(
        collection_name=collection, filter=expr,
        output_fields=output_fields or ["*"], limit=limit))

def delete_by_expr(collection: str, expr: str) -> int:
    """Delete by scalar filter"""
    res = get_milvus().delete(collection_name=collection, filter=expr)
    return int(res.get("delete_count", 0)) if isinstance(res, dict) else 0

def count_rows(collection: str) -> int:
    """Row count"""
    stats = get_milvus().get_collection_stats(collection_name=collection)
    return int(stats.get("row_count", 0)) if isinstance(stats, dict) else 0
