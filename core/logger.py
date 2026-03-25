# -*- encoding: utf-8 -*-
"""
@date: 2026/03/24
@file: logger.py
@author: GluttonousCat
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler


class LogColors:
    DEBUG = '\033[94m'
    INFO = '\033[92m'
    WARNING = '\033[93m'
    ERROR = '\033[91m'
    CRITICAL = '\033[1;91m'
    RESET = '\033[0m'


class LogManager:

    _initialized_loggers = {}

    LOG_DIR = Path("logs")
    DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    @classmethod
    def setup_logger(
            cls,
            name: str,
            level: int = logging.INFO,
            log_to_file: bool = True,
            log_file_name: str | None = None
    ) -> logging.Logger:

        if name in cls._initialized_loggers:
            return cls._initialized_loggers[name]

        logger = logging.getLogger(name)
        logger.setLevel(level)

        if logger.handlers:
            return logger

        formatter = logging.Formatter(
            cls.DEFAULT_FORMAT, datefmt=cls.DATE_FORMAT)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        if log_to_file:
            if not cls.LOG_DIR.exists():
                cls.LOG_DIR.mkdir(parents=True, exist_ok=True)

            file_path = cls.LOG_DIR / (log_file_name or f"{name}.log")

            file_handler = TimedRotatingFileHandler(
                filename=str(file_path),
                when='D',
                interval=1,
                backupCount=30,
                encoding='utf-8'
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        logger.propagate = False

        cls._initialized_loggers[name] = logger
        return logger


def get_logger(name: str = "App") -> logging.Logger:
    return LogManager.setup_logger(name)


if __name__ == "__main__":
    log = get_logger("DBClient")
    log.info("数据库连接成功")
    log.warning("连接池剩余空间不足")
    log.error("执行 SQL 失败: Select * from unknown_table")

    another_log = get_logger("DBClient")
    another_log.debug("这条 debug 默认不会显示，因为级别是 INFO")