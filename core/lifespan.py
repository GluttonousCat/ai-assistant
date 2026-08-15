# -*- encoding: utf-8 -*-
"""
FastAPI 应用生命周期管理
启动时初始化 MySQL 连接池, 关闭时释放资源
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 初始化与清理资源"""
    config = get_config()

    # 初始化 MySQL 连接池 (按需, 有密码才初始化)
    if config.mysql_config.get("password"):
        try:
            from storage.mysql import MysqlConnection
            MysqlConnection.init_pool(**config.mysql_config)
            logger.info("MySQL 连接池已初始化")
        except Exception as e:
            logger.warning(f"MySQL 连接池初始化失败 (非致命): {e}")

    logger.info(f"应用启动: {config.get('app.name', 'ai-assistant')} v{config.get('app.version', '2.0.0')}")
    yield

    logger.info("应用关闭")
