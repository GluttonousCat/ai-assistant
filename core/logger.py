# -*- encoding: utf-8 -*-
"""
Unified logging (aggregated into one file per top-level package)

Design (normalized 2026-09-10, replacing the old runaway one-file-per-module scheme):
- One log file per top-level package: logs/agent.log / mcp.log / core.log / api.log /
  storage.log / tools.log / jobs.log / evals.log /
  app.log — the module full name rides in the log line's %(name)s, so triage can
  still filter by module
- When run via `python -m`, __name__=="__main__"; take sys.modules['__main__'].__package__
  (set to the real package name under -m) and fall back to "cli" — kills the old
  catch-all __main__.log
- Level/dir/retention read from config.yaml logging section (level/dir/backup_count),
  defaults INFO/logs/30
- LOG_DIR anchors to the project root (walk up for config.yaml) so any CWD lands
  logs in the same place

Usage unchanged: from core.logger import get_logger; logger = get_logger(__name__)
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _project_root() -> Path:
    """Walk up for config.yaml to locate the project root (same rule as core.config._find_project_root)"""
    p = Path(__file__).resolve()
    for cand in (p.parents[1], *p.parents[2:7]):
        if (cand / "config.yaml").exists():
            return cand
    return p.parents[1]


def _settings() -> dict:
    """Logging config (level/dir/backup_count); defaults on read failure so logging never breaks business"""
    cfg = {"level": "INFO", "dir": "logs", "backup_count": 30}
    try:
        import yaml
        with open(_project_root() / "config.yaml", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        lg = data.get("logging") or {}
        for k in cfg:
            if lg.get(k) is not None:
                cfg[k] = lg[k]
    except Exception:  # noqa: BLE001
        pass
    return cfg


def _bucket(name: str) -> str:
    """Module full name -> top-level package name (log file bucket)"""
    if name == "__main__":
        pkg = getattr(sys.modules.get("__main__"), "__package__", "") or ""
        name = pkg or "cli"
    top = name.split(".")[0]
    return top if top.isidentifier() else "app"


class _LogManager:
    """Lazily create loggers per bucket (one file handler per bucket, idempotent in-process)"""

    _buckets: dict = {}

    @classmethod
    def setup(cls, name: str) -> logging.Logger:
        bucket = _bucket(name)
        if bucket not in cls._buckets:
            cfg = _settings()
            logger = logging.getLogger(bucket)
            logger.setLevel(
                getattr(logging, str(cfg["level"]).upper(), logging.INFO))
            formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(formatter)
            logger.addHandler(console)

            log_dir = _project_root() / str(cfg["dir"])
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = TimedRotatingFileHandler(
                filename=str(log_dir / f"{bucket}.log"),
                when="D", interval=1,
                backupCount=int(cfg["backup_count"]),
                encoding="utf-8")
            fh.setFormatter(formatter)
            logger.addHandler(fh)
            logger.propagate = False       # bucket is the terminal sink: no bubbling to root (prevents uvicorn double logging)
            cls._buckets[bucket] = logger

        # Return a child logger named after the module full name: level inherits
        # from the bucket, %(name)s prints the full name
        child = logging.getLogger(name)
        if name != bucket:
            child.propagate = True         # bubble up to the bucket logger for unified output
        return child


def get_logger(name: str = "app") -> logging.Logger:
    """Entry point: get_logger(__name__). Direct __main__ runs are bucketed and displayed under the real package name."""
    if name == "__main__":
        pkg = getattr(sys.modules.get("__main__"), "__package__", "") or ""
        name = pkg or "cli"          # %(name)s also prints the real package name, never __main__
    return _LogManager.setup(name)
