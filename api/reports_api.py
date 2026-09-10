# -*- encoding: utf-8 -*-
"""
研报中心 API
- GET  /api/reports                      研报列表 (分页/筛选: 标的/来源/分析状态)
- GET  /api/reports/{report_id}          研报详情 (含提取结果与预测数据)
- GET  /api/reports/{report_id}/download 下载研报原文文件 (本地已下载的 PDF/docx)
- POST /api/reports/{id}/analyze         触发单篇 LLM 结构化提取 (同步, 一次性返回)
- POST /api/reports/{id}/analyze/stream  同上, SSE 流式 (Agent 流程阶段 + LLM 输出逐块)
- POST /api/reports/analyze              批量提取未分析研报
- GET  /api/reports/stocks               有研报覆盖的标的列表 (研报热度)
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
from fastapi.security import HTTPBearer

from core.logger import get_logger
from core.security import TokenPayload
from api.deps import get_current_user, require_admin
from skills.base import SkillContext
from skills.report.skill import ReportSkill
from storage.pg import PgClient

logger = get_logger(__name__)
reports_router = APIRouter(prefix="/api/reports", tags=["reports"])

_bearer = HTTPBearer(auto_error=False)

# 标题常见前缀 (中文版-/【券商】-/券商-) 剥离后取 "券商-标的" 展示名
_ORG_PREFIX_RE = None  # lazy


@reports_router.get("")
async def list_reports(
    ts_code: Optional[str] = None,
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    analysis: Optional[str] = None,   # done / pending / all
    market: Optional[str] = None,     # A股 / 美股 / 港股 / 宏观 / 商品 / 行业 / 其他
    industry: Optional[str] = None,   # 行业标签 (tags_industries 精确匹配)
    region: Optional[str] = None,     # 地区标签
    commodity: Optional[str] = None,  # 商品标签
    theme: Optional[str] = None,      # 主题标签
    target: Optional[str] = None,     # 标的标签
    page: int = 1,
    page_size: int = 20,
    current: TokenPayload = Depends(get_current_user),
) -> Dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    where = ["1=1"]
    args: list = []
    # 只展示原始研报文件 (pdf/docx); 不展示话题文本解读版 (txt 易重复)
    # 排除 OCR 进行中的条目 (pending_ocr: 正文尚未就绪, 展示出来是空壳)
    if source:
        where.append("source = %s")
        args.append(source)
    else:
        where.append("(file_name ~* '\\.(pdf|docx?)$' OR (file_name IS NULL AND file_path IS NULL AND content_chars > 0))")
        where.append("extraction_status <> 'pending_ocr'")
    if market:
        where.append("market = %s")
        args.append(market)
    # 行业/地区: 独立列精确匹配 (LLM Analysis 提取); 兼容 tags 列 (深度提取多值)
    if industry:
        where.append("(industry = %s OR ',' || COALESCE(tags_industries,'') || ',' LIKE %s)")
        args.extend([industry, f"%,{industry},%"])
    if region:
        where.append("(region = %s OR ',' || COALESCE(tags_regions,'') || ',' LIKE %s)")
        args.extend([region, f"%,{region},%"])
    # 商品/主题/标的: tags 多值列
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
    """分类聚合: 行业/地区来自独立列 (LLM Analysis), 商品/主题/标的来自 tags 列 (深度提取)"""
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
    """研报覆盖统计 (按标的聚合, 含最新分析观点)"""
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
            "content_text, extraction_status, analysis_status, analysis_json, created_at "
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
    row["content_text"] = (row.get("content_text") or "")[:8000]
    row["forecasts"] = forecasts
    return row


@reports_router.get("/{report_id}/download")
async def download_report_file(report_id: int,
                               current: TokenPayload = Depends(get_current_user)):
    """下载研报原文 (本地已下载的 PDF/docx 原始文件)"""
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
    # RFC 5987: 中文文件名用 filename*=UTF-8''
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
    """单篇研报 LLM 结构化提取 (同步, 单篇秒级)"""
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
    单篇研报 AI 分析 (SSE 流式)。事件:
      stage {stage, message}   Agent 流程阶段 (读取正文/LLM提取/解析/入库)
      delta {text}             LLM 原始输出逐块 (多次)
      data  {data}             结构化结果摘要
      error {message}          失败
      done  {}                 结束 (error 后也会发, 便于前端统一收尾)
    Skill 生成器为同步重 IO (LLM/PG), 放 worker 线程执行, 事件经队列桥接到
    async 生成器 —— 不阻塞事件循环 (同 21:00 同步调度踩过的坑)。
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
            events.put(None)  # 结束哨兵

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
    """批量提取未分析研报 (后台线程, 立即返回)"""
    from fastapi.concurrency import run_in_threadpool

    def _job():
        try:
            _run_extract(None, limit)
        except Exception as e:
            logger.warning(f"批量研报提取失败: {e}")

    import threading
    threading.Thread(target=_job, daemon=True, name="report-extract").start()
    return {"status": "started", "limit": limit}
