# -*- encoding: utf-8 -*-
"""storage 包内置配置读取 (解耦合: 不依赖 core.config, 直接读进程 env)

- .env 兜底解析: 从仓库根 (本文件上级目录) 加载, 只补不覆盖 ——
  进程里已显式设置的环境变量优先; 应用侧 core.config 加载的是同一个 .env,
  先后加载结果一致, 双向兼容 (脚本独立跑 storage 也能拿到密钥)
- 连接配置构造: pg_config()/milvus_config(); PG_*/MILVUS_* 键名与默认值
  是全项目唯一事实源 (core.config 中无副本)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

_LOADED = False


def _load_dotenv() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    path = Path(__file__).resolve().parent.parent / ".env"  # storage/ 的上级 = 仓库根
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
    except Exception:
        pass


def env(key: str, default: Any = None) -> Any:
    """读环境变量 (.env 兜底, 进程显式设置优先)"""
    _load_dotenv()
    return os.environ.get(key, default)


def pg_config() -> Dict[str, Any]:
    """PostgreSQL 连接配置 (PG_HOST/PG_PORT/PG_USER/PG_PASSWORD/PG_DATABASE)"""
    return {
        "host": env("PG_HOST", "127.0.0.1"),
        "port": int(env("PG_PORT", 5432)),
        "user": env("PG_USER", "postgres"),
        "password": env("PG_PASSWORD", ""),
        "database": env("PG_DATABASE", "data"),
    }


def milvus_config() -> Dict[str, Any]:
    """Milvus 连接配置 (MILVUS_URI/MILVUS_TOKEN/MILVUS_DB/MILVUS_EMBED_DIM)"""
    return {
        "uri": env("MILVUS_URI", "http://127.0.0.1:19530"),
        "token": env("MILVUS_TOKEN", ""),
        "db_name": env("MILVUS_DB", "default"),
        # 向量维度与 embedding 模型强耦合 (换模型须重建 collection), 见 milvus_schema
        "embed_dim": int(env("MILVUS_EMBED_DIM", 1024)),
    }
