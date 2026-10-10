# -*- encoding: utf-8 -*-
"""
Milvus 向量库 Collection 定义 (角色对应 pg_schema.py 之于 PG)

分工: PG 存结构化事实 (fin.report_meta 一行 = 一篇研报), Milvus 存非结构化语义
(COLLECTION_REPORT_CHUNKS 一行 = 一个正文分块); report_id 是两库连接键 ——
PG 为主、Milvus 为从: 删研报须连带 delete_by_expr("report_id == ..."),
无外键级联, 由上层 (jobs/merge_report_duplicates 等) 负责成对清理。

与 PG 的关键差异 (DDL 语义):
- 维度不可 ALTER: embedding 维度 = MILVUS_EMBED_DIM, 与 embedding 模型强耦合,
  换模型必须 drop + rebuild + 重灌向量 (rebuild_report_chunks);
- 标量字段无 NULL: 未知的 ts_code/market 用空串 '' 哨兵, 过滤时注意
  (ts_code == "" 不是 IS NULL)。
"""
from __future__ import annotations

from typing import Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)

# 研报正文分块 (语义检索/RAG 的主 collection)
COLLECTION_REPORT_CHUNKS = "report_chunks"

# 分块正文上限 (VARCHAR max_length; 与上层分块器约定一致, 超长截断在分块时做)
CHUNK_TEXT_MAX = 16384


def build_report_chunks_schema(dim: int):
    """report_chunks 的字段定义 (确定性主键 "{report_id}:{chunk_idx}", upsert 幂等)"""
    from pymilvus import DataType
    from pymilvus import MilvusClient

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
    dim = dim or get_config().milvus_config["embed_dim"]
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
    dim = dim or get_config().milvus_config["embed_dim"]
    if client.has_collection(COLLECTION_REPORT_CHUNKS):
        client.drop_collection(COLLECTION_REPORT_CHUNKS)
        logger.warning(f"已 drop {COLLECTION_REPORT_CHUNKS} (维度变更重建)")
    ensure_report_chunks(client, dim)


# collection -> ensure 函数注册表 (storage.milvus 的通用执行器按名惰性 ensure, 进程内一次)
COLLECTION_ENSURES = {
    COLLECTION_REPORT_CHUNKS: ensure_report_chunks,
}
