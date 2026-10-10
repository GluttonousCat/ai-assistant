# -*- encoding: utf-8 -*-
from storage.pg import PgPool, PgClient, get_pg

__all__ = [
    "PgPool",
    "PgClient",
    "get_pg",
    "get_milvus",
    "close_milvus",
    "milvus_ready",
]

# Milvus 惰性导出 (PEP 562): 顶层 import 会与 `python -m storage.milvus` 冲突
# (runpy 要求模块未在包 __init__ 中被先行加载), 且未装 pymilvus 时不应拖垮 PG 导入
_MILVUS_EXPORTS = {"get_milvus", "close_milvus", "milvus_ready"}


def __getattr__(name):
    if name in _MILVUS_EXPORTS:
        import storage.milvus as _m
        return getattr(_m, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
