# -*- encoding: utf-8 -*-
"""
统一配置管理
- config.yaml: 非敏感配置
- .env:        敏感密钥 (OPENAI_API_KEY / TUSHARE_TOKEN / MYSQL_* ...)
优先级: 环境变量 > config.yaml
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def _find_project_root() -> Path:
    """向上查找包含 config.yaml 的项目根目录"""
    current = Path(__file__).resolve().parent
    for _ in range(6):
        if (current / "config.yaml").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """轻量 .env 加载 (不依赖 python-dotenv)"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class Config:
    """统一配置入口 (单例)"""

    _instance: Optional["Config"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "Config":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.root_dir = _find_project_root()
        _load_dotenv(self.root_dir / ".env")

        self._yaml: Dict[str, Any] = {}
        config_file = self.root_dir / "config.yaml"
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                self._yaml = yaml.safe_load(f) or {}

    # ---------- 通用读取 ----------
    def get(self, key: str, default: Any = None) -> Any:
        """读取 config.yaml, 支持点号路径: 'llm.model'"""
        value: Any = self._yaml
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value

    def env(self, key: str, default: Any = None) -> Any:
        """读取环境变量 (.env)"""
        return os.environ.get(key, default)

    # ---------- 常用快捷访问 ----------
    @property
    def openai_api_key(self) -> str:
        return self.env("OPENAI_API_KEY", "")

    @property
    def openai_base_url(self) -> str:
        return self.env("OPENAI_BASE_URL", "https://api.openai.com/v1")

    @property
    def llm_model(self) -> str:
        return self.env("LLM_MODEL", self.get("llm.models.default.model", "gpt-4o"))

    # ---------- LLM 多模型路由 ----------
    def llm_model_for(self, purpose: str) -> str:
        """按用途取模型名: llm.models.<purpose>.model, 不存在回落 default.
        环境变量 LLM_MODEL 兼容旧配置 (作为 default 的覆盖)."""
        if purpose in ("", "default"):
            return self.llm_model
        model = self.get(f"llm.models.{purpose}.model")
        return model if model else self.llm_model

    def llm_model_override(self, purpose: str) -> Optional[Dict[str, str]]:
        """按用途取 api_key/base_url 覆盖 (缺省用全局 .env 配置)"""
        if purpose in ("", "default"):
            return None
        entry = self.get(f"llm.models.{purpose}")
        if not isinstance(entry, dict):
            return None
        override: Dict[str, str] = {}
        for k in ("api_key", "base_url"):
            v = entry.get(k) or self.env(f"LLM_{purpose.upper()}_{k.upper()}", "")
            if v:
                override[k] = v
        return override or None

    @property
    def tushare_token(self) -> str:
        return self.env("TUSHARE_TOKEN", "")

    @property
    def mysql_config(self) -> Dict[str, Any]:
        return {
            "host": self.env("MYSQL_HOST", "127.0.0.1"),
            "port": int(self.env("MYSQL_PORT", 3306)),
            "user": self.env("MYSQL_USER", "root"),
            "password": self.env("MYSQL_PASSWORD", ""),
        }

    @property
    def mysql_database(self) -> str:
        return self.env("MYSQL_DATABASE", "stock")

    @property
    def pg_config(self) -> Dict[str, Any]:
        """PostgreSQL 连接配置"""
        return {
            "host": self.env("PG_HOST", "127.0.0.1"),
            "port": int(self.env("PG_PORT", 5432)),
            "user": self.env("PG_USER", "postgres"),
            "password": self.env("PG_PASSWORD", ""),
            "database": self.env("PG_DATABASE", "data"),
        }

    @property
    def pg_url(self) -> str:
        """SQLAlchemy PG 连接串 (保留密码原文, psycopg2 直接读 .env 参数)"""
        cfg = self.pg_config
        return (
            f"postgresql+psycopg2://{cfg['user']}:{cfg['password']}"
            f"@{cfg['host']}:{cfg['port']}/{cfg['database']}"
        )

    @property
    def mysql_url(self) -> str:
        """SQLAlchemy 连接串"""
        pwd = self.mysql_config["password"]
        # 转义 @ 等特殊字符
        pwd_escaped = pwd.replace("@", "%40")
        return (
            f"mysql+pymysql://{self.mysql_config['user']}:{pwd_escaped}"
            f"@{self.mysql_config['host']}:{self.mysql_config['port']}"
            f"/{self.mysql_database}"
        )

    @property
    def zsxq_cookie(self) -> str:
        return self.env("ZSXQ_COOKIE", "")

    @property
    def zsxq_group_id(self) -> str:
        return self.env("ZSXQ_GROUP_ID", "")


# 便捷函数
def get_config() -> Config:
    return Config()
