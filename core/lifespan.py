# -*- encoding: utf-8 -*-
"""
FastAPI 应用生命周期管理
启动时初始化 MySQL 连接池 + 每日增量同步调度器, 关闭时释放资源
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import get_config
from core.logger import get_logger
from core.scheduler import get_scheduler

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

    # 启动每日增量同步调度器 (交易日 schedule.sync_hour 自动回填, 可用 schedule.enabled 关闭)
    if config.get("schedule.enabled", True):
        try:
            scheduler = get_scheduler()
            scheduler.start()
            logger.info(
                f"每日增量同步调度器已启动 (交易日 "
                f"{config.get('schedule.sync_hour', 21)}:{config.get('schedule.sync_minute', 0):02d})"
            )
        except Exception as e:
            logger.warning(f"增量调度器启动失败 (非致命): {e}")

    logger.info(f"应用启动: {config.get('app.name', 'ai-assistant')} v{config.get('app.version', '2.0.0')}")
    yield

    # 停止调度器
    try:
        await get_scheduler().stop()
    except Exception:
        pass
    logger.info("应用关闭")
