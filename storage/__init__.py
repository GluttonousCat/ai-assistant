# -*- encoding: utf-8 -*-
from storage.pg import PgPool, PgClient, get_pg

__all__ = [
    "PgPool",
    "PgClient",
    "get_pg",
]

_MILVUS_EXPORTS = {"get_milvus", "close_milvus", "milvus_ready"}


def __getattr__(name):
    if name in _MILVUS_EXPORTS:
        import storage.milvus as _m
        return getattr(_m, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
