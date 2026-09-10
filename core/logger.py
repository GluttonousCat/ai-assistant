# -*- encoding: utf-8 -*-
"""
统一日志 (按顶层包聚合分文件)

设计 (2026-09-10 规范化, 替代旧的"一模块一文件"失控方案):
- 每个顶层包一个日志文件: logs/agent.log / mcp.log / core.log / api.log /
  storage.log / tools.log / range_trading.log / scripts.log / evals.log /
  app.log — 模块全名在日志行 %(name)s 里, 排查仍可按模块过滤
- `python -m` 直跑时 __name__=="__main__", 取 sys.modules['__main__'].__package__
  (-m 方式运行时已置好真实包名) 兜底 "cli" — 消灭旧 __main__.log 大锅饭
- 级别/目录/保留天数读 config.yaml logging 段 (level/dir/backup_count), 缺省 INFO/logs/30
- LOG_DIR 锚定项目根 (向上找 config.yaml), 任何 CWD 启动都落在同一处

用法不变: from core.logger import get_logger; logger = get_logger(__name__)
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 顶层包 → 日志文件名 白名单 (未命中的顶层名也允许, 天然兜底新包)
_KNOWN_TOPS = ("core", "skills", "services", "mcp", "api", "storage", "tools",
               "jobs", "range_trading", "evals", "app")


def _project_root() -> Path:
    """向上找 config.yaml 定位项目根 (与 core.config._find_project_root 同规则)"""
    p = Path(__file__).resolve()
    for cand in (p.parents[1], *p.parents[2:7]):
        if (cand / "config.yaml").exists():
            return cand
    return p.parents[1]


def _settings() -> dict:
    """logging 配置 (level/dir/backup_count), 读失败用缺省, 不让日志拖垮业务"""
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
    """模块全名 → 顶层包名 (日志文件桶)"""
    if name == "__main__":
        pkg = getattr(sys.modules.get("__main__"), "__package__", "") or ""
        name = pkg or "cli"
    top = name.split(".")[0]
    return top if top.isidentifier() else "app"


class _LogManager:
    """按桶懒建 logger (每桶一个文件 handler, 进程内幂等)"""

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
            logger.propagate = False       # 桶即终点, 不冒泡到 root (防 uvicorn 双打)
            cls._buckets[bucket] = logger

        # 返回「以模块全名为名」的子 logger: 级别继承自桶, %(name)s 打全名
        child = logging.getLogger(name)
        if name != bucket:
            child.propagate = True         # 冒泡到桶 logger 统一输出
        return child


def get_logger(name: str = "app") -> logging.Logger:
    """入口: get_logger(__name__)。__main__ 直跑自动按真实包名归桶+显示。"""
    if name == "__main__":
        pkg = getattr(sys.modules.get("__main__"), "__package__", "") or ""
        name = pkg or "cli"          # %(name)s 也打真实包名, 不再显示 __main__
    return _LogManager.setup(name)


# 兼容旧引用 (LogManager 曾被 core/__init__ 导出)
LogManager = _LogManager
