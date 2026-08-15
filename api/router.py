# -*- encoding: utf-8 -*-
"""
统一 API 路由
- /crawl/*       知识星球爬虫
- /files/*       文件下载
- /accounts/*    账号管理
- /agent/chat    Agent 对话 (阶段二接入)
- /health        健康检查
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Any

from fastapi import HTTPException, BackgroundTasks, APIRouter

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
from storage.sqlite.account import AccountDatabase

logger = get_logger(__name__)

router = APIRouter(tags=["router"])

# 运行期任务状态 (简易内存存储)
tasks: Dict[str, Dict[str, Any]] = {}


def get_crawler(group_id: str) -> ZSXQCrawler:
    """获取爬虫实例: 优先 .env Cookie, 其次 accounts.db"""
    config = get_config()
    cookie = config.zsxq_cookie

    if not cookie:
        account_db = AccountDatabase()
        account = account_db.get_default_account(mask_cookie=False)
        if account:
            cookie = account["cookie"]

    if not cookie:
        raise HTTPException(status_code=400, detail="未配置Cookie")

    return ZSXQCrawler(cookie, str(group_id))


# ---------- 基础 ----------
@router.get("/", tags=["meta"])
async def root():
    return {"message": "ai-assistant API", "version": "2.0.0"}


@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health():
    return HealthResponse(status="ok", time=datetime.now())


# ---------- 知识星球爬虫 ----------
@router.post("/crawl/latest", tags=["zsxq"])
async def crawl_latest(request: CrawlRequest, background_tasks: BackgroundTasks):
    """爬取最新话题"""
    def task():
        crawler = get_crawler(request.group_id)
        result = crawler.crawl_latest(request.per_page)
        crawler.close()
        return result

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始爬取最新话题"}


@router.post("/crawl/historical", tags=["zsxq"])
async def crawl_historical(request: CrawlRequest, background_tasks: BackgroundTasks):
    """爬取历史数据"""
    def task():
        crawler = get_crawler(request.group_id)
        result = crawler.crawl_historical(request.pages, request.per_page)
        crawler.close()
        return result

    background_tasks.add_task(task)
    return {"status": "started", "message": f"开始爬取{request.pages}页历史数据"}


@router.post("/crawl/incremental", tags=["zsxq"])
async def crawl_incremental(request: CrawlRequest, background_tasks: BackgroundTasks):
    """增量爬取"""
    def task():
        crawler = get_crawler(request.group_id)
        result = crawler.crawl_incremental(request.pages, request.per_page)
        crawler.close()
        return result

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始增量爬取"}


# ---------- 文件下载 ----------
@router.post("/files/collect", tags=["zsxq"])
async def collect_files(request: FileDownloadRequest, background_tasks: BackgroundTasks):
    """收集文件列表"""
    def task():
        config = get_config()
        cookie = config.zsxq_cookie
        downloader = FileDownloader(cookie, str(request.group_id))
        result = downloader.collect_files()
        downloader.close()
        return result

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始收集文件列表"}


@router.post("/files/download", tags=["zsxq"])
async def download_files(request: FileDownloadRequest, background_tasks: BackgroundTasks):
    """下载文件 - 限制每次1个"""
    def task():
        config = get_config()
        cookie = config.zsxq_cookie
        downloader = FileDownloader(cookie, str(request.group_id))
        result = downloader.download_pending(max_files=1)
        downloader.close()
        return result

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始下载文件（每次1个）"}


@router.get("/stats/{group_id}", tags=["zsxq"])
async def get_stats(group_id: str):
    """获取统计信息"""
    try:
        crawler = get_crawler(group_id)
        stats = crawler.db.get_stats()
        crawler.close()
        return {"group_id": group_id, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- 账号管理 ----------
@router.post("/accounts", tags=["zsxq"])
async def create_account(request: AccountCreateRequest):
    """创建账号"""
    db = AccountDatabase()
    account = db.add_account(request.cookie, request.name)
    return {"status": "ok", "account": account}


@router.get("/accounts", tags=["zsxq"])
async def list_accounts():
    """列出账号"""
    db = AccountDatabase()
    accounts = db.list_accounts()
    return {"accounts": accounts}


# ---------- Agent 对话 (阶段二接入) ----------
@router.post("/agent/chat", tags=["agent"])
async def agent_chat(request: dict):
    """
    Agent 对话入口 (阶段二实现)
    当前为占位, 将接入 LangGraph 主图
    """
    return {
        "status": "todo",
        "message": "Agent 对话将在阶段二接入",
        "received": request.get("text", ""),
    }
