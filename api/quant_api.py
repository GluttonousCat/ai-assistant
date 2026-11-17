"""Research reports and owner-scoped strategy authoring; simulations are queued."""

import asyncio
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from skills.quant import service
from skills.quant import workspace
from skills.quant.workspace_models import (
    StrategyDraft,
    BacktestRequest,
    GenerateRequest,
)
from tools.market import quant_workspace as store
from api.deps import get_current_user
from core.security import TokenPayload
from tools.market.backtest_data import workspace_availability

quant_router = APIRouter(prefix="/api/v1/quant", tags=["quant"])
_generation_slots = asyncio.Semaphore(1)


@quant_router.get("/workspace")
async def workspace_catalog(user: TokenPayload = Depends(get_current_user)):
    return await asyncio.to_thread(workspace.catalog, user.user_id)


@quant_router.get("/workspace/availability")
async def data_availability(user: TokenPayload = Depends(get_current_user)):
    try:
        row = await asyncio.to_thread(workspace_availability)
    except Exception as error:
        raise HTTPException(
            503, "数仓连接暂不可用，仍可编辑和保存策略；恢复连接后再运行回测"
        ) from error
    return {
        "sources": row,
        "available_end": min(row.values()) if row and all(row.values()) else None,
    }


@quant_router.post("/workspace/drafts", status_code=201)
async def save_draft(
    draft: StrategyDraft, user: TokenPayload = Depends(get_current_user)
):
    return await asyncio.to_thread(store.save_draft, user.user_id, draft.model_dump())


@quant_router.post("/workspace/generate")
async def generate_draft(
    request: GenerateRequest, user: TokenPayload = Depends(get_current_user)
):
    if _generation_slots.locked():
        raise HTTPException(429, "已有策略正在生成，请稍后重试")
    async with _generation_slots:
        try:
            return await asyncio.to_thread(workspace.generate, request)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except Exception as error:
            raise HTTPException(
                502, "策略生成暂不可用，请使用表单配置或稍后重试"
            ) from error


@quant_router.post("/workspace/tasks", status_code=202)
async def submit_backtest(
    request: BacktestRequest, user: TokenPayload = Depends(get_current_user)
):
    try:
        return await asyncio.to_thread(
            store.enqueue, user.user_id, request.model_dump(mode="json")
        )
    except store.QueueBusy as error:
        raise HTTPException(409, str(error)) from error
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@quant_router.get("/workspace/tasks/{task_id}")
async def workspace_task(task_id: UUID, user: TokenPayload = Depends(get_current_user)):
    row = await asyncio.to_thread(store.task, user.user_id, str(task_id))
    if not row:
        raise HTTPException(404, "未找到该回测任务")
    return row


@quant_router.get("/workspace/tasks/{task_id}/result")
async def workspace_result(
    task_id: UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=200),
    user: TokenPayload = Depends(get_current_user),
):
    row = await asyncio.to_thread(
        store.load_result, user.user_id, str(task_id), offset, limit
    )
    if row is None:
        raise HTTPException(404, "回测结果不存在或尚未完成")
    return row


@quant_router.get("/strategies")
async def strategies():
    return {"data": service.strategies()}


@quant_router.get("/backtests")
async def backtests(limit: int = Query(default=50, ge=1, le=100)):
    return {"data": await asyncio.to_thread(service.results, limit)}


@quant_router.get("/backtests/{run_id}")
async def backtest(
    run_id: UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
):
    result = await asyncio.to_thread(service.result, str(run_id), offset, limit)
    if result is None:
        raise HTTPException(404, "未找到该回测结果")
    return result
