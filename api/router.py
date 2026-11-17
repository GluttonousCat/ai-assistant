# -*- encoding: utf-8 -*-
"""
Unified API routes
- /crawl/*       ZSXQ (knowledge planet) crawler
- /files/*       File download
- /accounts/*    Account management
- /agent/chat    Agent chat (wired in phase 2)
- /health        Health check
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from typing import Dict, Any

from fastapi import HTTPException, BackgroundTasks, APIRouter, Depends

from api.deps import require_admin, get_operations

from api.schemas import (
    CrawlRequest,
    FileDownloadRequest,
    AccountCreateRequest,
    HealthResponse,
)
from core.config import get_config
from core.logger import get_logger
from tools.zsxq.crawler import ZSXQCrawler
from tools.zsxq.downloader import FileDownloader
from tools.zsxq import accounts
from tools.finance import report_catalog

logger = get_logger(__name__)

router = APIRouter(tags=["router"])

# Runtime task state (simple in-memory store)
tasks: Dict[str, Dict[str, Any]] = {}


def get_crawler(group_id: str) -> ZSXQCrawler:
    """Get a crawler instance: prefer the .env Cookie, then accounts.db"""
    config = get_config()
    cookie = config.zsxq_cookie

    if not cookie:
        cookie = accounts.default_cookie()

    if not cookie:
        raise HTTPException(status_code=400, detail="未配置Cookie")

    return ZSXQCrawler(cookie, str(group_id))


# ---------- Basics ----------
# The root path "/" is served by app.py as the frontend index (FileResponse);
# this router is mounted before the index route, so registering GET / here
# would shadow the index by registration order — hence no JSON root route here


@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health():
    return HealthResponse(status="ok", time=datetime.now())


# ---------- ZSXQ crawler ----------
@router.get("/crawl/status", tags=["zsxq"], dependencies=[Depends(require_admin)])
async def crawl_status():
    """Background task status (simple in-memory state)"""
    return {"tasks": tasks, "count": len(tasks)}


@router.post("/crawl/latest", tags=["zsxq"], dependencies=[Depends(require_admin)])
async def crawl_latest(request: CrawlRequest, background_tasks: BackgroundTasks):
    """Crawl the latest topics"""

    def task():
        with closing(get_crawler(request.group_id)) as crawler:
            return crawler.crawl_latest(request.per_page)

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始爬取最新话题"}


@router.post("/crawl/historical", tags=["zsxq"], dependencies=[Depends(require_admin)])
async def crawl_historical(request: CrawlRequest, background_tasks: BackgroundTasks):
    """Crawl historical data"""

    def task():
        with closing(get_crawler(request.group_id)) as crawler:
            return crawler.crawl_historical(request.pages, request.per_page)

    background_tasks.add_task(task)
    return {"status": "started", "message": f"开始爬取{request.pages}页历史数据"}


@router.post("/crawl/incremental", tags=["zsxq"], dependencies=[Depends(require_admin)])
async def crawl_incremental(request: CrawlRequest, background_tasks: BackgroundTasks):
    """Incremental crawl"""

    def task():
        with closing(get_crawler(request.group_id)) as crawler:
            return crawler.crawl_incremental(request.pages, request.per_page)

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始增量爬取"}


# ---------- File download ----------
@router.post("/files/collect", tags=["zsxq"], dependencies=[Depends(require_admin)])
async def collect_files(
    request: FileDownloadRequest, background_tasks: BackgroundTasks
):
    """Collect the file list"""

    def task():
        config = get_config()
        cookie = config.zsxq_cookie
        with closing(FileDownloader(cookie, str(request.group_id))) as downloader:
            return downloader.collect_files()

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始收集文件列表"}


@router.post("/files/download", tags=["zsxq"], dependencies=[Depends(require_admin)])
async def download_files(
    request: FileDownloadRequest, background_tasks: BackgroundTasks
):
    """Download files - limited to 1 per request"""

    def task():
        config = get_config()
        cookie = config.zsxq_cookie
        with closing(FileDownloader(cookie, str(request.group_id))) as downloader:
            return downloader.download_pending(max_files=1)

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始下载文件（每次1个）"}


@router.post(
    "/files/download-topic-files", tags=["zsxq"], dependencies=[Depends(require_admin)]
)
async def download_topic_files(
    request: FileDownloadRequest, background_tasks: BackgroundTasks
):
    """Download topic attachments (topic_files; reports are mostly topic attachments)"""

    def task():
        config = get_config()
        cookie = config.zsxq_cookie
        with closing(FileDownloader(cookie, str(request.group_id))) as downloader:
            return downloader.download_topic_files(max_files=request.max_files)

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始下载话题附件（每次1个）"}


@router.get("/stats/{group_id}", tags=["zsxq"], dependencies=[Depends(require_admin)])
def get_stats(group_id: str):
    """Get crawl statistics"""
    try:
        with closing(get_crawler(group_id)) as crawler:
            stats = crawler.db.get_stats()
        return {"group_id": group_id, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- Report management (fin.report_meta) ----------
@router.get("/reports", tags=["zsxq"], dependencies=[Depends(require_admin)])
def list_reports(source: str = None, status: str = None, limit: int = 100):
    """Report list (query on fin.report_meta)"""
    return report_catalog.legacy_reports(source, status, limit)


@router.get(
    "/reports/{report_id}", tags=["zsxq"], dependencies=[Depends(require_admin)]
)
def get_report(report_id: int, include_text: bool = False):
    """Report detail (full text omitted by default)"""
    try:
        return report_catalog.legacy_detail(report_id, include_text)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/reports/upload", tags=["zsxq"], dependencies=[Depends(require_admin)])
def upload_report(request: dict):
    """
    Manually upload report text into the DB (one of the report verification data sources)
    body: {"ts_code": "600519.SH", "title": "...", "content_text": "...", "source": "upload"}
    """
    ts_code = request.get("ts_code")
    title = (request.get("title") or "").strip()
    content = (request.get("content_text") or "").strip()

    if not content:
        raise HTTPException(status_code=400, detail="content_text 不能为空")

    return report_catalog.add_report(ts_code, title, content)


@router.post(
    "/crawl/sync-reports", tags=["zsxq"], dependencies=[Depends(require_admin)]
)
def sync_zxsq_reports(
    group_id: str = None,
    background_tasks: BackgroundTasks = None,
    operations=Depends(get_operations),
):
    """Sync locally crawled zsxq data into the PG report store"""

    def task():
        gid = group_id or get_config().zsxq_group_id
        if not gid:
            raise HTTPException(status_code=400, detail="未指定群组ID")
        operations.sync_reports(gid)

    if background_tasks:
        background_tasks.add_task(task)
        return {"status": "started", "message": "后台开始同步研报数据"}
    else:
        # Foreground sync (for debugging)
        task()
        return {"status": "ok", "message": "同步完成"}


# ---------- Account management ----------
@router.post("/accounts", tags=["zsxq"], dependencies=[Depends(require_admin)])
def create_account(request: AccountCreateRequest):
    """Create an account"""
    account = accounts.create_account(request.cookie, request.name)
    return {"status": "ok", "account": account}


@router.get("/accounts", tags=["zsxq"], dependencies=[Depends(require_admin)])
def list_accounts():
    """List accounts"""
    return {"accounts": accounts.list_accounts()}


# ---------- Agent chat (wired in phase 2) ----------
@router.post("/agent/chat", tags=["agent"], dependencies=[Depends(require_admin)])
async def agent_chat(request: dict):
    """
    Agent chat entry (phase 2)
    Currently a placeholder; will be wired to the LangGraph main graph
    """
    return {
        "status": "todo",
        "message": "Agent 对话将在阶段二接入",
        "received": request.get("text", ""),
    }


# ---------- Data sync scheduling ----------
@router.post("/admin/sync-today", tags=["admin"], dependencies=[Depends(require_admin)])
async def sync_today(operations=Depends(get_operations)):
    """Manually trigger the daily job scheduled at 17:00 Asia/Shanghai."""
    result = await operations.trigger_sync()
    return {"status": result["status"], "result": result}


@router.get(
    "/admin/scheduler-status", tags=["admin"], dependencies=[Depends(require_admin)]
)
async def scheduler_status(operations=Depends(get_operations)):
    """Scheduler status"""
    return operations.scheduler_status()
