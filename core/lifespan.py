# -*- encoding: utf-8 -*-
"""
FastAPI application lifecycle management
On startup: init auth schema + daily incremental sync schedulers; on shutdown: release resources
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import get_config
from core.logger import get_logger
from jobs.scheduler import get_scheduler

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: initialize and clean up resources"""
    config = get_config()

    # Init auth (idempotently create auth.users + seed admin; failure does not block
    # startup — the error surfaces at login time)
    try:
        from core import security
        security.ensure_auth_schema()
        logger.info("认证模块已初始化 (auth.users)")
    except Exception as e:
        logger.warning(f"认证初始化失败 (登录功能不可用): {e}")

    # Start the daily incremental sync scheduler (auto backfills trading days at
    # schedule.sync_hour; can be disabled via schedule.enabled)
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

    # Start the zsxq report crawler scheduler (daily 07:00/23:00 fetch + sync +
    # LLM extraction, with a startup catch-up pass as a safety net)
    if config.get("zsxq_schedule.enabled", True):
        try:
            from jobs.zsxq_scheduler import get_zsxq_scheduler
            get_zsxq_scheduler().start()
            logger.info("知识星球研报抓取调度器已启动 (每日 07:00/23:00)")
        except Exception as e:
            logger.warning(f"知识星球调度器启动失败 (非致命): {e}")

    # Start the daily 21:30 range+trend scan scheduler (the day's daily bars are
    # already synced by the 21:00 job)
    try:
        from api.range_api import start_scheduler as start_range_scheduler
        start_range_scheduler()
    except Exception as e:
        logger.warning(f"区间扫描调度器启动失败 (非致命): {e}")

    # Detect staleness at startup and catch up (handles timers missed by shutdown/sleep)
    try:
        from api.range_api import catchup_if_stale
        catchup_if_stale()
    except Exception as e:
        logger.warning(f"启动补跑检查失败 (非致命): {e}")

    logger.info(f"应用启动: {config.get('app.name', 'ai-assistant')} v{config.get('app.version', '2.0.0')}")
    yield

    # Stop schedulers
    try:
        await get_scheduler().stop()
    except Exception:
        pass
    try:
        from jobs.zsxq_scheduler import get_zsxq_scheduler
        await get_zsxq_scheduler().stop()
    except Exception:
        pass
    logger.info("应用关闭")
