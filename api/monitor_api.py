# -*- encoding: utf-8 -*-
"""
Agent 实时监控 API (SSE)

- POST /api/monitor/stream  每 2s 推一帧全量状态 (SSE, 与 agent/stream 同协议族)
- GET  /api/monitor/state   一次性快照 (首屏/调试)
状态 = core.metrics 进程内指标 (会话/漏斗/工具/LLM) + PG 侧数据新鲜度 (10s 缓存,
含 PG 延迟/研报管线/同步水位线)。整组 require_admin (DB 实时角色校验)。
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

monitor_router = APIRouter(prefix="/monitor", tags=["monitor"])

_PG_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def _pg_state() -> Dict[str, Any]:
    """PG 侧状态 (延迟/研报管线/同步水位线); 10s 缓存, SSE 帧间不重复查库"""
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
    except Exception as e:  # noqa: BLE001 PG 不可达也是监控要展示的状态
        data = {"ok": False, "error": str(e)[:80]}
    _PG_CACHE["ts"] = time.time()
    _PG_CACHE["data"] = data
    return data


def _build_state() -> Dict[str, Any]:
    return {"metrics": snapshot(), "pg": _pg_state()}


@monitor_router.get("/state")
async def monitor_state(current: TokenPayload = Depends(require_admin)):
    return _build_state()


@monitor_router.post("/stream")
async def monitor_stream(current: TokenPayload = Depends(require_admin)):
    async def gen():
        while True:
            yield f"data: {json.dumps(_build_state(), ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
