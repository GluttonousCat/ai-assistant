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
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from api.deps import require_admin
from core.security import TokenPayload
from core.logger import get_logger
from core.metrics import snapshot

logger = get_logger(__name__)

monitor_router = APIRouter(prefix="/api/monitor", tags=["monitor"])

_PG_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def _pg_state() -> Dict[str, Any]:
    """PG-side state (latency/report pipeline/sync watermarks); cached 10s so SSE frames don't re-query the DB"""
    if time.time() - _PG_CACHE["ts"] < 10:
        return _PG_CACHE["data"] or {}
    t0 = time.time()
    try:
        from storage.pg import PgClient
        with PgClient() as pg:
            pipeline = pg.fetch_one(
                "SELECT count(*) total,"
                " count(*) FILTER (WHERE analysis_status='done') done,"
                " count(*) FILTER (WHERE analysis_status='failed') failed,"
                " count(*) FILTER (WHERE extraction_status='pending_ocr') ocr,"
                " count(*) FILTER (WHERE content_text_cn IS NOT NULL) translated"
                " FROM fin.report_meta")
            freshness = pg.fetch_all(
                "SELECT table_name, updated_at FROM fin.sync_meta"
                " ORDER BY updated_at DESC LIMIT 8")
            quote = pg.fetch_one(
                "SELECT max(trade_date)::text d FROM stock.daily")
        data = {
            "ok": True, "latency_ms": int((time.time() - t0) * 1000),
            "pipeline": dict(pipeline) if pipeline else {},
            "freshness": [dict(r) for r in freshness],
            "last_trade_date": quote["d"] if quote else None,
        }
    except Exception as e:  # noqa: BLE001 PG being unreachable is itself a state monitoring must show
        data = {"ok": False, "error": str(e)[:80]}
    _PG_CACHE["ts"] = time.time()
    _PG_CACHE["data"] = data
    return data


def _build_state() -> Dict[str, Any]:
    return {"state": {"metrics": snapshot(), "pg": _pg_state()}}


@monitor_router.get("/state")
async def monitor_state(current: TokenPayload = Depends(require_admin)):
    return _build_state()["state"]


@monitor_router.post("/stream")
async def monitor_stream(current: TokenPayload = Depends(require_admin)):
    async def gen():
        while True:
            # PG probing is synchronous psycopg2; run it in a thread pool to avoid blocking the event loop (AGENTS heavy-sync-IO pitfall)
            payload = await asyncio.to_thread(_build_state)
            yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
