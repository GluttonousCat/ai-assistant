# -*- encoding: utf-8 -*-
"""
Unified configuration management
- config.yaml: non-sensitive config
- .env:        secrets (OPENAI_API_KEY / TUSHARE_TOKEN / PG_* / ZSXQ_* ...)
Priority: environment variables > config.yaml
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def _find_project_root() -> Path:
    """Walk upward to locate the project root containing config.yaml"""
    current = Path(__file__).resolve().parent
    for _ in range(6):
        if (current / "config.yaml").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Lightweight .env loader (no python-dotenv dependency)"""
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
    """Unified configuration entry (singleton)"""

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

    # ---------- generic getters ----------
    def get(self, key: str, default: Any = None) -> Any:
        """Read config.yaml; supports dot paths: 'llm.model'"""
        value: Any = self._yaml
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value

    def env(self, key: str, default: Any = None) -> Any:
        """Read an environment variable (.env)"""
        return os.environ.get(key, default)

    # ---------- common shortcuts ----------
    @property
    def openai_api_key(self) -> str:
        return self.env("OPENAI_API_KEY", "")

    @property
    def openai_base_url(self) -> str:
        return self.env("OPENAI_BASE_URL", "https://api.openai.com/v1")

    @property
    def llm_model(self) -> str:
        return self.env("LLM_MODEL", self.get("llm.models.default.model", "gpt-4o"))

    # ---------- LLM multi-model routing ----------
    def llm_model_for(self, purpose: str) -> str:
        """Get model name by purpose: llm.models.<purpose>.model, falls back to default when absent.
        Env var LLM_MODEL stays compatible with legacy config (overrides default)."""
        if purpose in ("", "default"):
            return self.llm_model
        model = self.get(f"llm.models.{purpose}.model")
        return model if model else self.llm_model

    def llm_model_override(self, purpose: str) -> Optional[Dict[str, str]]:
        """Get per-purpose api_key/base_url override (defaults to the global .env config)"""
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
    def wecom_bot_id(self) -> str:
        return self.env("WECOM_BOT_ID", "")

    @property
    def wecom_bot_secret(self) -> str:
        return self.env("WECOM_BOT_SECRET", "")

    # PG/Milvus connection config has been inlined into the storage package
    # (pg.py: pg_config / milvus.py: milvus_config, reads process env directly)

    @property
    def zsxq_cookie(self) -> str:
        return self.env("ZSXQ_COOKIE", "")

    @property
    def zsxq_group_id(self) -> str:
        return self.env("ZSXQ_GROUP_ID", "")


# Convenience function
def get_config() -> Config:
    return Config()
