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
import json
import os
import queue as _queue
import threading
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from core.logger import get_logger
from core.security import TokenPayload
from api.deps import get_current_user, require_admin
from skills.base import SkillContext
from skills.report.skill import ReportSkill
from storage.pg import PgClient

logger = get_logger(__name__)
reports_router = APIRouter(prefix="/api/reports", tags=["reports"])


@reports_router.get("")
async def list_reports(
    ts_code: Optional[str] = None,
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    analysis: Optional[str] = None,   # done / pending / all
    market: Optional[str] = None,     # A-shares / US stocks / HK stocks / macro / commodity / industry / other
    industry: Optional[str] = None,   # industry tag (exact match on tags_industries)
    region: Optional[str] = None,     # region tag
    commodity: Optional[str] = None,  # commodity tag
    theme: Optional[str] = None,      # theme tag
    target: Optional[str] = None,     # target tag
    page: int = 1,
    page_size: int = 20,
    current: TokenPayload = Depends(get_current_user),
) -> Dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    where = ["1=1"]
    args: list = []
    # Show only original report files (pdf/docx); hide topic-text digest versions (txt duplicates easily)
    # Exclude entries still undergoing OCR (pending_ocr: body not ready, they would render as empty shells)
    if source:
        where.append("source = %s")
        args.append(source)
    else:
        where.append("(file_name ~* '\\.(pdf|docx?)$' OR (file_name IS NULL AND file_path IS NULL AND content_chars > 0))")
        where.append("extraction_status <> 'pending_ocr'")
    if market:
        where.append("market = %s")
        args.append(market)
    # Industry/region: exact match on dedicated columns (from LLM Analysis); also matches the tags columns (multi-value, from deep extraction)
    if industry:
        where.append("(industry = %s OR ',' || COALESCE(tags_industries,'') || ',' LIKE %s)")
        args.extend([industry, f"%,{industry},%"])
    if region:
        where.append("(region = %s OR ',' || COALESCE(tags_regions,'') || ',' LIKE %s)")
        args.extend([region, f"%,{region},%"])
    # Commodity/theme/target: multi-value tags columns
    for col, val in (
        ("tags_commodities", commodity),
        ("tags_themes", theme),
        ("tags_targets", target),
    ):
        if val:
            where.append(f"(',' || COALESCE({col},'') || ',' LIKE %s)")
            args.append(f"%,{val},%")
    if keyword:
        where.append("(title ILIKE %s OR COALESCE(author,'') ILIKE %s OR COALESCE(org_name,'') ILIKE %s)")
        args.extend([f"%{keyword}%"] * 3)
    if analysis == "done":
        where.append("analysis_status = 'done'")
    elif analysis == "pending":
        where.append("(analysis_status IS NULL OR analysis_status NOT IN ('done','failed'))")
    where_sql = " AND ".join(where)

    with PgClient() as pg:
        total = pg.fetch_one(
            f"SELECT COUNT(*) AS n FROM fin.report_meta WHERE {where_sql}",
            tuple(args))["n"]
        rows = pg.fetch_all(
            f"SELECT report_id, topic_id, ts_code, title, author, org_name, "
            f"target, industry, region, market, "
            f"publish_date, report_type, source, file_name, content_chars, "
            f"extraction_status, analysis_status, symbols, "
            f"tags_industries, tags_regions, tags_commodities, tags_themes, tags_targets, "
            f"created_at "
            f"FROM fin.report_meta WHERE {where_sql} "
            f"ORDER BY COALESCE(publish_date, created_at::date) DESC, report_id DESC "
            f"LIMIT %s OFFSET %s",
            tuple(args) + (page_size, (page - 1) * page_size))
    for r in rows:
        if r.get("publish_date"):
            r["publish_date"] = r["publish_date"].strftime("%Y-%m-%d")
        if r.get("created_at"):
            r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M")
    return {"total": total, "page": page, "page_size": page_size, "items": rows}


@reports_router.get("/tags")
async def report_tags(current: TokenPayload = Depends(get_current_user)):
    """Tag aggregates: industries/regions from dedicated columns (LLM Analysis); commodities/themes/targets from tags columns (deep extraction)"""
    with PgClient() as pg:
        industries = pg.fetch_all(
            "SELECT industry AS tag, COUNT(*) AS n FROM fin.report_meta "
            "WHERE industry IS NOT NULL AND industry <> '' GROUP BY 1 ORDER BY n DESC LIMIT 60")
        regions = pg.fetch_all(
            "SELECT region AS tag, COUNT(*) AS n FROM fin.report_meta "
            "WHERE region IS NOT NULL AND region <> '' GROUP BY 1 ORDER BY n DESC LIMIT 60")

        def _unnest(col: str) -> str:
            return (f"SELECT trim(split_part(t.v, ',', n.n)) AS tag, COUNT(*) AS n "
                    f"FROM (SELECT COALESCE({col},'') AS v FROM fin.report_meta "
                    f"WHERE analysis_status='done') t, "
                    f"generate_series(1, array_length(string_to_array(t.v, ','), 1)) AS n(n) "
                    f"WHERE trim(split_part(t.v, ',', n.n)) <> '' "
                    f"GROUP BY 1 ORDER BY n DESC LIMIT 60")
        commodities = pg.fetch_all(_unnest("tags_commodities"))
        themes = pg.fetch_all(_unnest("tags_themes"))
        targets = pg.fetch_all(_unnest("tags_targets"))
    return {"industries": industries, "regions": regions,
            "commodities": commodities, "themes": themes, "targets": targets}


@reports_router.get("/stocks")
async def report_stocks(current: TokenPayload = Depends(get_current_user)):
    """Report coverage stats (aggregated by target, incl. latest analysis view)"""
    with PgClient() as pg:
        rows = pg.fetch_all(
            """
            SELECT m.ts_code, MAX(b.name) AS name, COUNT(*) AS report_count,
                   MAX(COALESCE(m.publish_date, m.created_at::date)) AS latest_date,
                   (SELECT COUNT(*) FROM fin.report_forecast f
                     WHERE f.ts_code = m.ts_code) AS forecast_count
            FROM fin.report_meta m
            LEFT JOIN stock.stock_basic b ON b.ts_code = m.ts_code
            WHERE m.ts_code IS NOT NULL
            GROUP BY m.ts_code
            ORDER BY report_count DESC
            LIMIT 200
            """)
    for r in rows:
        if r.get("latest_date"):
            r["latest_date"] = r["latest_date"].strftime("%Y-%m-%d")
    return {"items": rows}


@reports_router.get("/{report_id}")
async def report_detail(report_id: int,
                        current: TokenPayload = Depends(get_current_user)):
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT report_id, topic_id, file_id, ts_code, title, author, org_name, "
            "publish_date, report_type, source, file_name, file_path, content_chars, "
              "content_text, content_text_cn, extraction_status, analysis_status, "
              "analysis_json, created_at "
            "FROM fin.report_meta WHERE report_id=%s", (report_id,))
        if not row:
            raise HTTPException(status_code=404, detail="研报不存在")
        forecasts = pg.fetch_all(
            "SELECT forecast_type, forecast_period, forecast_value, forecast_unit, "
            "confidence, raw_text FROM fin.report_forecast WHERE report_id=%s "
            "ORDER BY forecast_period, forecast_type", (report_id,))
    for k in ("publish_date", "created_at"):
        if row.get(k):
            row[k] = row[k].strftime("%Y-%m-%d %H:%M") if k == "created_at" \
                else row[k].strftime("%Y-%m-%d")
    # Prefer the translation when an English original has one (content_text_cn is empty for Chinese bodies, naturally falls back to the original text)
    row["content_text"] = ((row.pop("content_text_cn", None) or row.get("content_text") or "")
                           )[:8000]
    row["forecasts"] = forecasts
    return row


@reports_router.get("/{report_id}/download")
async def download_report_file(report_id: int,
                               current: TokenPayload = Depends(get_current_user)):
    """Download the original report (locally downloaded PDF/docx file)"""
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT file_name, file_path FROM fin.report_meta WHERE report_id=%s",
            (report_id,))
    if not row:
        raise HTTPException(status_code=404, detail="研报不存在")
    path = row.get("file_path")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail="原文文件不存在 (话题文本类研报无原文文件, 或本地文件已清理)")
    fname = row.get("file_name") or os.path.basename(path)
    # RFC 5987: non-ASCII filenames go in filename*=UTF-8''
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(fname)}"})


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
async def analyze_report(report_id: int,
                         current: TokenPayload = Depends(get_current_user)):
    """Single-report LLM structured extraction (sync, seconds per report)"""
    try:
        result = await asyncio.to_thread(_run_extract, report_id, 1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@reports_router.post("/{report_id}/analyze/stream")
async def analyze_report_stream(report_id: int,
                                current: TokenPayload = Depends(get_current_user)):
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

    threading.Thread(target=_worker, daemon=True,
                     name=f"report-stream-{report_id}").start()

    async def _gen():
        while True:
            try:
                ev = events.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.08)
                continue
            if ev is None:
                break
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
async def analyze_batch(limit: int = 10,
                        current: TokenPayload = Depends(require_admin)):
    """Batch-extract unanalyzed reports (background thread, returns immediately)"""
    from fastapi.concurrency import run_in_threadpool

    def _job():
        try:
            _run_extract(None, limit)
        except Exception as e:
            logger.warning(f"批量研报提取失败: {e}")

    import threading
    threading.Thread(target=_job, daemon=True, name="report-extract").start()
    return {"status": "started", "limit": limit}
