# -*- encoding: utf-8 -*-
"""
Milvus 向量库底层封装 (角色对应 pg.py 之于 PG)

- 单例连接: MilvusClient 进程内一份 (gRPC 长连接, 自身线程安全,
  无连接耗尽问题 —— 不需要 PG 式连接池)
- 惰性连接 + 探活: milvus_ready() 在服务端未起时返回 False 而非抛错,
  向量检索属增强功能, 服务不在时上层降级、不阻塞主链路 (PG 不可用才是故障)
- 通用执行器: insert/upsert/search/delete/query/count;
  collection 的字段定义在 storage.milvus_schema (业务语义不在这层)
- 向量化不在这层: embedding 由上层 (skill/tool) 用模型生成后传入

服务端部署 (Windows 无 Milvus Lite, 须 Docker 跑 standalone):
    官方 standalone compose (etcd+minio+milvus 三容器) 见 Milvus 文档;
    起来后 .env 配 MILVUS_URI=http://127.0.0.1:19530 即可。

用法:
    python -m storage.milvus           # 探活 + collection 状态汇总
    python -m storage.milvus --smoke   # 全链路冒烟: 假向量写入 -> 检索 -> 删除
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Sequence

from core.config import get_config
from core.logger import get_logger

from storage.milvus_schema import COLLECTION_ENSURES

logger = get_logger(__name__)

_client = None
_lock = threading.Lock()
_not_ready_warned = False
# 进程内已 ensure 过的 collection (与 pg_schema._report_analysis_cols_ensured 同款惰性)
_ensured: set = set()


def get_milvus():
    """取 Milvus 单例客户端 (惰性连接; 服务端不可达时抛 RuntimeError 附中文指引)"""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        from pymilvus import MilvusClient
        cfg = get_config().milvus_config
        kwargs: Dict[str, Any] = {"uri": cfg["uri"], "db_name": cfg["db_name"]}
        if cfg["token"]:
            kwargs["token"] = cfg["token"]
        try:
            _client = MilvusClient(**kwargs)
            _client.list_collections()  # 探活: 连接串错/服务未起在此暴露
        except Exception as e:
            _client = None
            raise RuntimeError(
                f"Milvus 不可达 ({cfg['uri']}): {e} —— "
                f"检查服务端是否已启动/.env MILVUS_URI; 未部署时向量功能自动降级") from e
        logger.info(f"Milvus 已连接: {cfg['uri']}/{cfg['db_name']}")
        return _client


def close_milvus() -> None:
    """关闭连接 (进程收尾/测试用; 日常无需手动调)"""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
            _client = None


def milvus_ready() -> bool:
    """探活: 服务端在则 True; 不在则告警一次并返回 False (上层据此降级)"""
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
    """按注册表惰性建 collection (进程内一次; 未注册的名字直接放行, 由调用方自管)"""
    if collection in _ensured:
        return
    ensure_fn = COLLECTION_ENSURES.get(collection)
    if ensure_fn is not None:
        ensure_fn(get_milvus())
    _ensured.add(collection)


# ---------- 通用执行器 (字段语义见 milvus_schema) ----------

def insert_rows(collection: str, rows: List[Dict[str, Any]]) -> int:
    """插入 (主键重复会报错; 灌库幂等请用 upsert_rows)"""
    if not rows:
        return 0
    _ensure(collection)
    res = get_milvus().insert(collection_name=collection, data=rows)
    return int(res.get("insert_count", len(rows))) if isinstance(res, dict) else len(rows)


def upsert_rows(collection: str, rows: List[Dict[str, Any]]) -> int:
    """按主键 upsert (确定性 chunk_id 下的幂等重灌)"""
    if not rows:
        return 0
    _ensure(collection)
    res = get_milvus().upsert(collection_name=collection, data=rows)
    return int(res.get("upsert_count", len(rows))) if isinstance(res, dict) else len(rows)


def search_similar(collection: str, vector: Sequence[float], limit: int = 5,
                   expr: Optional[str] = None,
                   output_fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """向量近邻检索, 返回平铺行 [{主键id, distance, **标量字段}...] (COSINE, 越小越近)"""
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
    """标量过滤查询 (无向量), 如 report_id == 123 的全部分块"""
    _ensure(collection)
    return list(get_milvus().query(
        collection_name=collection, filter=expr,
        output_fields=output_fields or ["*"], limit=limit))


def delete_by_expr(collection: str, expr: str) -> int:
    """按标量过滤删除 (如 report_id == 123; PG 侧删研报时须连带调用)"""
    _ensure(collection)
    res = get_milvus().delete(collection_name=collection, filter=expr)
    return int(res.get("delete_count", 0)) if isinstance(res, dict) else 0


def count_rows(collection: str) -> int:
    """行数 (来自 collection 统计, 新写入未 flush 时可能略滞后)"""
    _ensure(collection)
    stats = get_milvus().get_collection_stats(collection_name=collection)
    return int(stats.get("row_count", 0)) if isinstance(stats, dict) else 0


# ---------- 冒烟 ----------

def _status() -> None:
    ready = milvus_ready()
    print(f"Milvus 探活: {'OK' if ready else '不可达 (向量功能将降级)'}")
    if not ready:
        return
    client = get_milvus()
    cfg = get_config().milvus_config
    print(f"uri={cfg['uri']} db={cfg['db_name']} embed_dim={cfg['embed_dim']}")
    for name in sorted(COLLECTION_ENSURES):
        exists = client.has_collection(name)
        n = count_rows(name) if exists else "-"
        print(f"  {name}: {'存在' if exists else '未建'}, 行数={n}")


def _smoke() -> int:
    """假向量全链路: 写入 -> 检索 -> 清理 (服务端须已启动)"""
    import random

    from storage.milvus_schema import COLLECTION_REPORT_CHUNKS

    if not milvus_ready():
        print("Milvus 不可达, 冒烟终止 (exit 1)")
        return 1
    dim = get_config().milvus_config["embed_dim"]
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
        print(f"写入 {n} 行, 检索命中 {len(hits)} (top1 distance={hits[0]['distance']:.4f})"
              if hits else "检索未命中")
        deleted = delete_by_expr(COLLECTION_REPORT_CHUNKS, f'chunk_id == "{chunk_id}"')
        print(f"清理 {deleted} 行 —— 冒烟通过")
        return 0
    except Exception as e:
        print(f"冒烟失败: {e}")
        return 1


if __name__ == "__main__":
    import sys

    if "--smoke" in sys.argv:
        sys.exit(_smoke())
    _status()
