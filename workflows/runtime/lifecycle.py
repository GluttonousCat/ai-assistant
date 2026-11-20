# -*- encoding: utf-8 -*-
"""Application startup and shutdown composition for authentication and jobs."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI

from core.config import get_config
from core.logger import get_logger
from workflows.market.daily_scheduler import get_scheduler
from workflows.market.digest_scheduler import get_digest_scheduler, get_midday_scheduler
from workflows.market.intraday_scheduler import get_intraday_scheduler
from workflows.reports.morning_scheduler import get_report_digest_scheduler
from workflows.runtime.operations import BackendOperations

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    app.state.operations = BackendOperations()
    from workflows.quant.worker import run_worker
    from workflows.reports.scheduler import get_offpeak_scheduler, get_zsxq_scheduler
    from tools.auth.users import ensure_auth_schema

    quant_stop = asyncio.Event()
    quant_task = asyncio.create_task(run_worker(quant_stop))
    started = []
    try:
        try:
            ensure_auth_schema()
            logger.info("认证模块已初始化 (auth.users)")
        except Exception as error:
            logger.warning("认证初始化失败 (登录功能不可用): %s", error)

        schedulers = (
            (get_scheduler, "日线增量同步", config.get("schedule.enabled", True)),
            (get_intraday_scheduler, "盘中60F", True),
            (get_digest_scheduler, "趋势邮件", True),
            (get_midday_scheduler, "午间60F邮件", True),
            (get_report_digest_scheduler, "研报观点早报", True),
            (get_zsxq_scheduler, "知识星球研报", config.get("zsxq_schedule.enabled", True)),
            (get_offpeak_scheduler, "闲时模型解析", config.get("offpeak_processing.enabled", True)),
        )
        for factory, label, enabled in schedulers:
            if not enabled:
                continue
            try:
                scheduler = factory()
                scheduler.start()
                started.append((scheduler, label))
            except Exception as error:
                logger.warning("%s调度器启动失败: %s", label, type(error).__name__)

        logger.info(
            "应用启动: %s v%s",
            config.get("app.name", "ai-assistant"),
            config.get("app.version", "2.0.0"),
        )
        yield
    finally:
        quant_stop.set()
        try:
            await quant_task
        finally:
            for scheduler, label in reversed(started):
                try:
                    await scheduler.stop()
                except Exception as error:
                    logger.warning("%s调度器停止失败: %s", label, type(error).__name__)
            logger.info("应用关闭")
