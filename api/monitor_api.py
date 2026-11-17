# -*- encoding: utf-8 -*-
"""
Real-time Agent monitoring API (SSE)

- POST /api/monitor/stream  Push a full state frame every 2s (SSE, same protocol family as agent/stream)
- GET  /api/monitor/state   One-shot snapshot (first paint / debugging)
State = in-process metrics from core.metrics (sessions/funnel/tools/LLM) + PG-side
data freshness (10s cache, incl. PG latency/report pipeline/sync watermarks).
The whole group is require_admin (live DB role check).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from api.deps import require_admin
from core.security import TokenPayload
from core.logger import get_logger
from core.metrics import snapshot
from tools.monitoring import database_snapshot as _pg_state

logger = get_logger(__name__)

monitor_router = APIRouter(prefix="/api/monitor", tags=["monitor"])


def _build_state() -> Dict[str, Any]:
    return {"state": {"metrics": snapshot(), "pg": _pg_state()}}


@monitor_router.get("/state")
async def monitor_state(current: TokenPayload = Depends(require_admin)):
    return (await asyncio.to_thread(_build_state))["state"]


@monitor_router.post("/stream")
async def monitor_stream(current: TokenPayload = Depends(require_admin)):
    async def gen():
        while True:
            # PG probing is synchronous psycopg2; run it in a thread pool to avoid blocking the event loop (AGENTS heavy-sync-IO pitfall)
            payload = await asyncio.to_thread(_build_state)
            yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
