# -*- encoding: utf-8 -*-

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pymilvus import DataType, MilvusClient

logger = logging.getLogger(__name__)

_yaml_milvus_cache: Optional[Dict[str, Any]] = None


def _yaml_milvus() -> Dict[str, Any]:
    """config.yaml 的 milvus 段 (进程内读一次; 文件缺失/无该段返回空 dict)"""
    global _yaml_milvus_cache
    if _yaml_milvus_cache is None:
        _yaml_milvus_cache = {}
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data.get("milvus"), dict):
                _yaml_milvus_cache = data["milvus"]
        except Exception as e:
            logger.warning(f"config.yaml 读取失败 (Milvus 配置仅用 env/默认值): {e}")
    return _yaml_milvus_cache


def milvus_config() -> Dict[str, Any]:
    """Milvus 连接配置: env MILVUS_* > config.yaml milvus 段 > 默认值 (项目约定 env > yaml)

    yaml 可配: uri / db_name / embed_dim; token 属敏感信息, 建议只放 .env (MILVUS_TOKEN)。
    """
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
    """配置检测: 返回问题清单 (空列表 = 通过)"""
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

COLLECTION_REPORT_CHUNKS = "report_chunks"

CHUNK_TEXT_MAX = 16384


def build_report_chunks_schema(dim: int):
    """report_chunks 的字段定义 (确定性主键 "{report_id}:{chunk_idx}", upsert 幂等)"""
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("report_id", DataType.INT64)                  # -> fin.report_meta.report_id
    schema.add_field("chunk_idx", DataType.INT64)                  # 分块序号 (0 起)
    schema.add_field("ts_code", DataType.VARCHAR, max_length=16)   # '' = 非A股/未知
    schema.add_field("market", DataType.VARCHAR, max_length=8)     # 词表同 fin.report_meta.market
    schema.add_field("org_name", DataType.VARCHAR, max_length=64)  # 券商 (检索回显)
    schema.add_field("publish_year", DataType.INT32)               # 0 = 未知 (无 DATE 类型)
    schema.add_field("chunk_text", DataType.VARCHAR, max_length=CHUNK_TEXT_MAX)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    return schema


def _vector_index_params(client):
    """向量索引: HNSW + COSINE (文本语义检索默认; 数据量到百万级再谈 IVF)"""
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )
    return index_params

def ensure_report_chunks(client, dim: Optional[int] = None) -> bool:
    """幂等建 collection; 返回是否本次新建。

    已存在且维度一致 -> 跳过; 维度不一致 -> 告警并指导 rebuild (不自动删数据)。
    """
    dim = dim or milvus_config()["embed_dim"]
    if client.has_collection(COLLECTION_REPORT_CHUNKS):
        desc = client.describe_collection(COLLECTION_REPORT_CHUNKS)
        for f in desc.get("fields", []):
            if f.get("name") == "embedding":
                cur_dim = (f.get("params") or {}).get("dim")
                if cur_dim != dim:
                    logger.warning(
                        f"{COLLECTION_REPORT_CHUNKS} 维度 {cur_dim} != 配置 {dim} "
                        f"(换过 embedding 模型?); 需 rebuild_report_chunks 重建并重灌")
                break
        return False
    client.create_collection(
        collection_name=COLLECTION_REPORT_CHUNKS,
        schema=build_report_chunks_schema(dim),
        index_params=_vector_index_params(client),
    )
    logger.info(f"已创建 Milvus collection {COLLECTION_REPORT_CHUNKS} (dim={dim})")
    return True


def rebuild_report_chunks(client, dim: Optional[int] = None) -> None:
    """drop + 重建 (换 embedding 模型/维度后用; 原向量全部作废, 须重灌)"""
    dim = dim or milvus_config()["embed_dim"]
    if client.has_collection(COLLECTION_REPORT_CHUNKS):
        client.drop_collection(COLLECTION_REPORT_CHUNKS)
        logger.warning(f"已 drop {COLLECTION_REPORT_CHUNKS} (维度变更重建)")
    ensure_report_chunks(client, dim)


COLLECTION_ENSURES = {
    COLLECTION_REPORT_CHUNKS: ensure_report_chunks,
}
