# -*- encoding: utf-8 -*-
"""report_chunks collection — 研报分块语义检索的业务定义 (字段/生命周期)

契约:
- PG 为主 Milvus 为从: report_id 连 fin.report_meta.report_id,
  删研报须连带 delete_report_chunks(report_id) (尽力而为, 不阻断主流程);
- 维度 = milvus_config()["embed_dim"], 与 embedding 模型强耦合,
  换模型须 rebuild_report_chunks() 重建并重灌;
- 标量无 NULL: 未知 ts_code 用 '', 未知 publish_year 用 0;
- 切分契约 (入库须遵守): 清洗剥页眉页脚/免责声明尾部; 表格线性化文本
  不入 embedding (数字断行是毒药, 占位"[表格: …]"代替); 重切算法变更后
  重灌须先 delete_report_chunks 清孤儿 (chunk_idx 会整体位移)。
"""
from __future__ import annotations

import logging
from functools import lru_cache

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

COLLECTION_REPORT_CHUNKS = "report_chunks"

CHUNK_TEXT_MAX = 16384


def _schema(dim: int):
    """字段定义 (确定性主键 "{report_id}:{chunk_idx}", upsert 幂等)"""
    schema = new_schema()
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("report_id", DataType.INT64)                 # -> fin.report_meta.report_id
    schema.add_field("chunk_idx", DataType.INT64)                 # 分块序号 (0 起)
    schema.add_field("ts_code", DataType.VARCHAR, max_length=16)  # '' = 非A股/未知
    schema.add_field("market", DataType.VARCHAR, max_length=8)    # 词表同 fin.report_meta.market
    schema.add_field("org_name", DataType.VARCHAR, max_length=64)  # 券商 (检索回显)
    schema.add_field("publish_year", DataType.INT32)              # 0 = 未知 (无 DATE 类型)
    schema.add_field("section", DataType.VARCHAR, max_length=256)  # 标题路径 (回显+过滤)
    schema.add_field("chunk_type", DataType.VARCHAR, max_length=16)  # text/table/chart
    schema.add_field("chunk_text", DataType.VARCHAR, max_length=CHUNK_TEXT_MAX)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    return schema


@lru_cache(maxsize=1)
def _ensure_created() -> bool:
    client = get_milvus()
    dim = milvus_config()["embed_dim"]
    cur_dim = collection_vector_dim(client, COLLECTION_REPORT_CHUNKS)
    if cur_dim is not None and cur_dim != dim:
        logger.warning(
            f"{COLLECTION_REPORT_CHUNKS} 维度 {cur_dim} != 配置 {dim} "
            f"(换过 embedding 模型?); 需 rebuild_report_chunks() 重建并重灌")
    return ensure_collection(client, COLLECTION_REPORT_CHUNKS,
                             _schema(dim), vector_index_params(client))


def ensure_report_chunks() -> bool:
    """幂等建 collection (进程内一次); 返回是否本次新建"""
    return _ensure_created()


def rebuild_report_chunks() -> None:
    """drop + 重建 (换 embedding 模型/维度后; 原向量作废须重灌)"""
    client = get_milvus()
    dim = milvus_config()["embed_dim"]
    rebuild_collection(client, COLLECTION_REPORT_CHUNKS,
                       _schema(dim), vector_index_params(client))
    _ensure_created.cache_clear()


def delete_report_chunks(report_id: int) -> int:
    """删单篇研报的全部向量 (PG 侧删除时联动)。

    尽力而为: Milvus 不可达时告警不阻断 (merge 主流程不因向量层故障中断;
    孤儿向量无害 — 检索回显 report_id 查不到 PG 时上层丢弃)。
    """
    try:
        return delete_by_expr(COLLECTION_REPORT_CHUNKS,
                              f"report_id == {report_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清理 {COLLECTION_REPORT_CHUNKS} report_id={report_id} "
                       f"失败 (不阻断): {e}")
        return 0
