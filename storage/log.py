# -*- encoding: utf-8 -*-
"""storage 包内置轻量日志 (解耦合: 本包不依赖 core.logger)

- 控制台 (stderr) 输出, 级别由 env STORAGE_LOG_LEVEL 控制 (默认 INFO)
- 包内所有 storage.* 子 logger 共享一个 handler, 并关闭 propagate,
  宿主应用 (core.logger/root) 已配置 handler 时不会重复打印
- 需要落盘时由宿主进程重定向 stderr (项目惯例: 服务输出重定向文件)
"""
from __future__ import annotations

import logging
import os
import sys

_configured = False


def get_logger(name: str) -> logging.Logger:
    """取包内 logger (幂等配置, 首次调用挂载 handler)"""
    global _configured
    if not _configured:
        level = (os.environ.get("STORAGE_LOG_LEVEL") or "INFO").upper()
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        pkg = logging.getLogger("storage")
        pkg.addHandler(handler)
        pkg.setLevel(getattr(logging, level, logging.INFO))
        pkg.propagate = False
        _configured = True
    return logging.getLogger(name)
