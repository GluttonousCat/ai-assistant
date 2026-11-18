# -*- encoding: utf-8 -*-
"""
Administrator-only task, log and Agent monitoring API.

- POST /api/monitor/stream  Push a full state frame every 2s (SSE, same protocol family as agent/stream)
- GET  /api/monitor/state   One-shot snapshot (first paint / debugging)
State includes durable local work observations and an asynchronous PG probe.
The whole group is require_admin (live DB role check).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from api.deps import require_admin
from core.security import TokenPayload
from core.logger import get_logger
from core.metrics import snapshot
from tools.monitoring import (
    cached_database_snapshot as _pg_state,
    background_snapshot,
    read_logs,
)

logger = get_logger(__name__)

monitor_router = APIRouter(prefix="/api/monitor", tags=["monitor"])


def _build_state() -> Dict[str, Any]:
    return {
        "state": {
            "metrics": snapshot(),
            "pg": _pg_state(),
            "background": background_snapshot(),
        }
    }


@monitor_router.get("/logs")
async def monitor_logs(
    source: str = Query("jobs", max_length=20),
    level: str = Query("ALL", max_length=10),
    search: str = Query("", max_length=120),
    limit: int = Query(300, ge=1, le=1000),
    current: TokenPayload = Depends(require_admin),
):
    try:
        return await asyncio.to_thread(
            read_logs, source=source, level=level, search=search, limit=limit
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=503, detail="日志文件暂时无法读取") from error


@monitor_router.get("/state")
async def monitor_state(current: TokenPayload = Depends(require_admin)):
    return (await asyncio.to_thread(_build_state))["state"]


@monitor_router.post("/stream")
async def monitor_stream(current: TokenPayload = Depends(require_admin)):
    async def gen():
        while True:
            # Local file reads stay off the event loop; PG uses one shared probe.
            payload = await asyncio.to_thread(_build_state)
            yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
