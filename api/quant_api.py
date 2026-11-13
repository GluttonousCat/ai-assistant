"""JWT-protected read-only strategy/backtest endpoints; no request-time compute."""

import asyncio
from uuid import UUID
from fastapi import APIRouter, HTTPException, Query
from skills.quant import service

quant_router = APIRouter(prefix="/api/v1/quant", tags=["quant"])


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
