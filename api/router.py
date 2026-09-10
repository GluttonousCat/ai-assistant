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
from storage.pg import PgClient
from storage.pg_schema import T_REPORT_META, T_REPORT_FORECAST

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
# 根路径 "/" 由 app.py 提供前端首页 (FileResponse); 本 router 先于首页路由挂载,
# 此处若注册 GET / 会按注册顺序抢占首页, 因此不再提供 JSON 版根路由

@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health():
    return HealthResponse(status="ok", time=datetime.now())


# ---------- 知识星球爬虫 ----------
@router.get("/crawl/status", tags=["zsxq"])
async def crawl_status():
    """后台任务状态 (简单内存态)"""
    return {"tasks": tasks, "count": len(tasks)}


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


@router.post("/files/download-topic-files", tags=["zsxq"])
async def download_topic_files(request: FileDownloadRequest, background_tasks: BackgroundTasks):
    """下载话题附件 (topic_files, 研报多为话题附件)"""
    def task():
        config = get_config()
        cookie = config.zsxq_cookie
        downloader = FileDownloader(cookie, str(request.group_id))
        result = downloader.download_topic_files(max_files=request.max_files)
        downloader.close()
        return result

    background_tasks.add_task(task)
    return {"status": "started", "message": "开始下载话题附件（每次1个）"}


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


# ---------- 研报管理 (fin.report_meta) ----------
@router.get("/reports", tags=["zsxq"])
async def list_reports(source: str = None, status: str = None, limit: int = 100):
    """研报列表 (fin.report_meta 查询)"""
    sql = f"""SELECT report_id, topic_id, file_id, ts_code, title, source,
                     extraction_status, content_chars, publish_date, created_at
              FROM {T_REPORT_META} WHERE 1=1"""
    params: list = []
    if source:
        sql += " AND source = %s"
        params.append(source)
    if status:
        sql += " AND extraction_status = %s"
        params.append(status)
    sql += " ORDER BY report_id DESC LIMIT %s"
    params.append(min(limit, 500))

    with PgClient() as pg:
        rows = pg.fetch_all(sql, params)
        total = pg.fetch_one(f"SELECT count(*) AS c FROM {T_REPORT_META}")["c"]
    return {"total": total, "reports": rows}


@router.get("/reports/{report_id}", tags=["zsxq"])
async def get_report(report_id: int, include_text: bool = False):
    """研报详情 (默认不带全文)"""
    with PgClient() as pg:
        row = pg.fetch_one(
            f"SELECT * FROM {T_REPORT_META} WHERE report_id = %s", (report_id,))
        if not row:
            raise HTTPException(status_code=404, detail="研报不存在")
        if not include_text:
            row.pop("content_text", None)
    return row


@router.post("/reports/upload", tags=["zsxq"])
async def upload_report(request: dict):
    """
    手动上传研报文本入库 (研报校验数据源之一)
    body: {"ts_code": "600519.SH", "title": "...", "content_text": "...", "source": "upload"}
    """
    ts_code = request.get("ts_code")
    title = (request.get("title") or "").strip()
    content = (request.get("content_text") or "").strip()

    if not content:
        raise HTTPException(status_code=400, detail="content_text 不能为空")

    with PgClient() as pg:
        cursor = pg.conn.cursor()
        cursor.execute(
            f"""INSERT INTO {T_REPORT_META}
                (ts_code, title, content_text, content_chars, source, extraction_status)
                VALUES (%s, %s, %s, %s, 'upload', 'extracted') RETURNING report_id""",
            (ts_code, title if title else content[:80], content, len(content)),
        )
        report_id = cursor.fetchone()[0]

    return {"status": "ok", "report_id": report_id}


@router.post("/crawl/sync-reports", tags=["zsxq"])
async def sync_zxsq_reports(group_id: str = None, background_tasks: BackgroundTasks = None):
    """将本地爬取的 zsxq 数据同步到 PG 研报库"""
    def task():
        from jobs.sync_zxsq_to_pg import ZSXQ2PGSync
        gid = group_id or get_config().zsxq_group_id
        if not gid:
            raise HTTPException(status_code=400, detail="未指定群组ID")
        sync = ZSXQ2PGSync(gid)
        sync.run()

    if background_tasks:
        background_tasks.add_task(task)
        return {"status": "started", "message": "后台开始同步研报数据"}
    else:
        # 前台同步 (调试用)
        task()
        return {"status": "ok", "message": "同步完成"}


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


# ---------- 数据同步调度 ----------
@router.post("/admin/sync-today", tags=["admin"])
async def sync_today():
    """手动触发每日增量同步 (交易日 18:00 自动执行的同款)"""
    from jobs.scheduler import get_scheduler
    result = await get_scheduler().trigger_now()
    return {"status": "done", "result": result}


@router.get("/admin/scheduler-status", tags=["admin"])
async def scheduler_status():
    """调度器状态"""
    from jobs.scheduler import get_scheduler
    s = get_scheduler()
    return {
        "running": s._task is not None and not s._task.done(),
        "last_run": s.last_run.isoformat() if s.last_run else None,
        "last_result": s.last_result,
    }
