# -*- encoding: utf-8 -*-
"""
Report center API
- GET  /api/reports                      Report list (paginated/filters: target/source/analysis status)
- GET  /api/reports/{report_id}          Report detail (incl. extraction results and forecast data)
- GET  /api/reports/{report_id}/download Download the original report file (locally downloaded PDF/docx)
- POST /api/reports/{id}/analyze         Trigger single-report LLM structured extraction (sync, one-shot return)
- POST /api/reports/{id}/analyze/stream  Same as above but SSE streaming (Agent pipeline stages + LLM output chunks)
- POST /api/reports/analyze              Batch-extract unanalyzed reports
- GET  /api/reports/stocks               Targets with report coverage (report heat)
"""

from __future__ import annotations

import asyncio
import os
import queue as _queue
import threading
from time import monotonic
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from core.logger import get_logger
from core.offpeak_queue import enqueue
from core.security import TokenPayload
from api.events import sse as _sse
from api.deps import get_current_user, require_admin
from skills.base import SkillContext
from skills.report.skill import ReportSkill
from tools.finance import report_catalog as catalog

logger = get_logger(__name__)
reports_router = APIRouter(prefix="/api/reports", tags=["reports"])


@reports_router.get("")
async def list_reports(
    ts_code: Optional[str] = None,
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    analysis: Optional[str] = None,  # done / pending / all
    market: Optional[
        str
    ] = None,  # A-shares / US stocks / HK stocks / macro / commodity / industry / other
    industry: Optional[str] = None,  # industry tag (exact match on tags_industries)
    region: Optional[str] = None,  # region tag
    commodity: Optional[str] = None,  # commodity tag
    theme: Optional[str] = None,  # theme tag
    target: Optional[str] = None,  # target tag
    page: int = 1,
    page_size: int = 20,
    current: TokenPayload = Depends(get_current_user),
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        catalog.list_reports,
        ts_code=ts_code,
        keyword=keyword,
        source=source,
        analysis=analysis,
        market=market,
        industry=industry,
        region=region,
        commodity=commodity,
        theme=theme,
        target=target,
        page=page,
        page_size=page_size,
    )


@reports_router.get("/tags")
async def report_tags(current: TokenPayload = Depends(get_current_user)):
    """Tag aggregates: industries/regions from dedicated columns (LLM Analysis); commodities/themes/targets from tags columns (deep extraction)"""
    return await asyncio.to_thread(catalog.report_tags)


@reports_router.get("/stocks")
async def report_stocks(current: TokenPayload = Depends(get_current_user)):
    """Report coverage stats (aggregated by target, incl. latest analysis view)"""
    return await asyncio.to_thread(catalog.report_stocks)


@reports_router.get("/{report_id}")
async def report_detail(
    report_id: int, current: TokenPayload = Depends(get_current_user)
):
    try:
        return await asyncio.to_thread(catalog.report_detail, report_id=report_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@reports_router.get("/{report_id}/download")
async def download_report_file(
    report_id: int, current: TokenPayload = Depends(get_current_user)
):
    """Download the original report (locally downloaded PDF/docx file)"""
    row = await asyncio.to_thread(catalog.report_file, report_id)
    if not row:
        raise HTTPException(status_code=404, detail="研报不存在")
    path = row.get("file_path")
    if not path or not await asyncio.to_thread(os.path.exists, path):
        raise HTTPException(
            status_code=404,
            detail="原文文件不存在 (话题文本类研报无原文文件, 或本地文件已清理)",
        )
    fname = row.get("file_name") or os.path.basename(path)
    # RFC 5987: non-ASCII filenames go in filename*=UTF-8''
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


def _run_extract(report_id: Optional[int], limit: int) -> Dict[str, Any]:
    skill = ReportSkill()
    params: Dict[str, Any] = {"mode": "extract", "limit": limit}
    if report_id:
        params["report_id"] = report_id
    ctx = skill(SkillContext(user_input="", params=params))
    if ctx.error:
        raise RuntimeError(ctx.error)
    return ctx.result or {"summary": "done"}


@reports_router.post("/{report_id}/analyze")
async def analyze_report(
    report_id: int, current: TokenPayload = Depends(get_current_user)
):
    """Single-report LLM structured extraction (sync, seconds per report)"""
    try:
        result = await asyncio.to_thread(_run_extract, report_id, 1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


@reports_router.post("/{report_id}/analyze/stream")
async def analyze_report_stream(
    report_id: int, current: TokenPayload = Depends(get_current_user)
):
    """
    Single-report AI analysis (SSE streaming). Events:
      stage {stage, message}   Agent pipeline stage (read body/LLM extract/parse/persist)
      delta {text}             Raw LLM output in chunks (multiple)
      data  {data}             Structured result summary
      error {message}          Failure
      done  {}                 End (also sent after error so the frontend can wrap up uniformly)
    The skill generator is synchronous heavy IO (LLM/PG); it runs in a worker
    thread and events bridge to the async generator via a queue — the event loop
    is not blocked (same pitfall as the 21:00 sync scheduler).
    """
    events: "_queue.Queue[Optional[Dict[str, Any]]]" = _queue.Queue()

    def _worker():
        skill = ReportSkill()
        try:
            for ev in skill.extract_stream(report_id):
                events.put(ev)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"研报 #{report_id} AI 分析流式执行失败")
            events.put({"type": "error", "message": str(e)})
        finally:
            events.put(None)  # end sentinel

    threading.Thread(
        target=_worker, daemon=True, name=f"report-stream-{report_id}"
    ).start()

    async def _gen():
        next_keepalive = monotonic() + 15
        while True:
            try:
                ev = events.get_nowait()
            except _queue.Empty:
                if monotonic() >= next_keepalive:
                    yield ": keepalive\n\n"
                    next_keepalive = monotonic() + 15
                await asyncio.sleep(0.08)
                continue
            if ev is None:
                break
            next_keepalive = monotonic() + 15
            etype = ev.pop("type", "message")
            if etype == "error":
                yield _sse("error", ev)
                break
            yield _sse(etype, ev)
        yield _sse("done", {})

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@reports_router.post("/analyze")
async def analyze_batch(
    limit: int = Query(10, ge=1, le=100), current: TokenPayload = Depends(require_admin)
):
    """Queue a durable batch for the nightly worker."""
    job_id = await asyncio.to_thread(enqueue, "report_batch", limit=limit)
    return {"status": "queued", "job_id": job_id, "limit": limit}
