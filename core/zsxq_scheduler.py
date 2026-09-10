# -*- encoding: utf-8 -*-
"""
知识星球研报每日抓取调度器 (asyncio 后台任务, 随后端常驻)

调度规则:
- 每天 07:00 与 23:00 各执行一轮 (07:00 覆盖前一天晚间~今晨的内容, 23:00 覆盖当天)
- 每轮流程:
    1. 爬取最新话题 (增量, 研报核心观点文本附在话题里)
    2. 收集文件列表 (增量) + 下载待下载文件 (限额, 反检测间隔)
    3. 同步到 PG 研报库 (fin.report_meta / report_forecast)
    4. 对未分析研报跑 LLM 结构化提取 (限额, 控制成本)
- 幂等: 重复执行安全 (话题/文件按 ID 查重, PG 同步断点续传, 提取按 analysis_status)
- 启动补跑: 若开机晚于 07:00 且当天首轮未执行 (用户 ~6:30 开机, 服务器随登录自启),
  启动时立即补跑一轮, 保证早上打开平台就有最新研报
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)

# 调度时点 (可通过 config.yaml zsxq_schedule 覆盖)
RUN_HOURS = [7, 23]
# 每轮 LLM 提取上限 (控制 token 成本; 23:00 那轮多跑一些)
EXTRACT_LIMITS = {7: 8, 23: 15}
# 每轮文件下载上限
MAX_FETCH_FILES = 15
# 每轮 LLM Analysis (轻量元数据) 上限 — 秒级/篇, 放宽
META_ANALYSIS_LIMIT = 40


class ZSXQScheduleScheduler:
    """知识星球研报每日抓取调度器"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.last_run: Optional[datetime] = None
        self.last_result: Optional[dict] = None
        # state key -> 完成日期 (用于启动补跑判断: 今天该跑的轮次是否已跑)
        self._done: dict = {}

        cfg = get_config()
        hours = cfg.get("zsxq_schedule.run_hours")
        if isinstance(hours, list) and hours:
            self.run_hours = sorted(int(h) for h in hours)
        else:
            self.run_hours = RUN_HOURS

    # ---------- 时间计算 ----------
    def _next_run(self, now: Optional[datetime] = None) -> datetime:
        """下一个调度时点 (今天未到的最早时点, 否则明天的第一个时点)"""
        now = now or datetime.now()
        for h in self.run_hours:
            target = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if now < target:
                return target
        return (now + timedelta(days=1)).replace(
            hour=self.run_hours[0], minute=0, second=0, microsecond=0)

    def _seconds_until(self, target: datetime) -> float:
        return max(0.0, (target - datetime.now()).total_seconds())

    # ---------- 单轮执行 ----------
    def _run_round(self, hour: int) -> dict:
        """同步执行一轮抓取 (在线程池中运行)"""
        cfg = get_config()
        group_id = cfg.zsxq_group_id
        if not group_id:
            return {"error": "未配置 ZSXQ_GROUP_ID"}
        cookie = cfg.zsxq_cookie
        if not cookie:
            from storage.sqlite.account import AccountDatabase
            account = AccountDatabase().get_default_account(mask_cookie=False)
            cookie = account["cookie"] if account else ""
        if not cookie:
            return {"error": "未配置 ZSXQ_COOKIE"}

        result = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "group": group_id, "hour": hour}

        # 1+2: 爬话题 + 收集/下载文件 (复用 daily_fetch_zxsq.run_once)
        try:
            from scripts.daily_fetch_zxsq import run_once
            fetch = run_once(
                str(group_id),
                max_fetch=MAX_FETCH_FILES,
                max_pages=10,
                crawl_topics=True,
            )
            result["fetch"] = {k: v for k, v in fetch.items()
                               if k in ("topics", "collect", "download", "pg_sync")}
        except Exception as e:
            logger.error(f"知识星球抓取失败: {e}")
            result["fetch"] = {"error": str(e)}

        # 3: LLM Analysis 元数据提取 (机构/标的/行业/地区/市场 -> 表格列; 轻量秒级)
        try:
            from tools.finance.report_meta_analysis import analyze_pending
            if get_config().openai_api_key:
                n = analyze_pending(limit=META_ANALYSIS_LIMIT)
                result["meta_analysis"] = {"processed": n}
            else:
                result["meta_analysis"] = {"skipped": "未配置 OPENAI_API_KEY"}
        except Exception as e:
            logger.error(f"LLM Analysis 失败: {e}")
            result["meta_analysis"] = {"error": str(e)}

        # 4: LLM 深度提取 (评级/盈利预测/观点/tags)
        try:
            from agent.skills.base import SkillContext
            from agent.skills.report.skill import ReportSkill
            limit = EXTRACT_LIMITS.get(hour, 8)
            if get_config().openai_api_key:
                ctx = ReportSkill()(SkillContext(
                    user_input="", params={"mode": "extract", "limit": limit}))
                result["extract"] = ctx.result or {"error": ctx.error}
            else:
                result["extract"] = {"skipped": "未配置 OPENAI_API_KEY"}
        except Exception as e:
            logger.error(f"研报 LLM 提取失败: {e}")
            result["extract"] = {"error": str(e)}

        self.last_run = datetime.now()
        self.last_result = result
        return result

    async def _run_round_async(self, hour: int) -> dict:
        """异步包装: 重活放线程池, 不阻塞事件循环"""
        return await asyncio.to_thread(self._run_round, hour)

    # ---------- 启动补跑 ----------
    async def _catchup_if_missed(self):
        """
        启动补跑: 用户 ~6:30 开机, 服务器随登录自启.
        若启动时已过今日某调度时点且该轮未执行 -> 立即补跑最近错过的一轮.
        (23:00 的轮次由常驻进程正常触发, 一般不会错过; 主要兜 07:00)
        """
        now = datetime.now()
        today = now.date()
        # 找今天已到点但未跑的最近一轮
        missed_hour = None
        for h in reversed(self.run_hours):
            if now >= now.replace(hour=h, minute=0, second=0, microsecond=0):
                if self._done.get(f"{today}:{h}") is None:
                    missed_hour = h
                break
        if missed_hour is None:
            return
        logger.info(f"[知识星球] 启动补跑: 今日 {missed_hour}:00 轮次未执行, 立即补跑")
        try:
            result = await self._run_round_async(missed_hour)
            self._done[f"{today}:{missed_hour}"] = datetime.now()
            logger.info(f"[知识星球] 补跑完成: { {k: v for k, v in result.items() if k != 'fetch'} }")
        except Exception as e:
            logger.error(f"[知识星球] 补跑失败: {e}")

    # ---------- 调度循环 ----------
    async def _loop(self):
        logger.info(f"[知识星球] 研报抓取调度器已启动 (每日 {'/'.join(f'{h:02d}:00' for h in self.run_hours)})")
        await self._catchup_if_missed()

        while not self._stop_event.is_set():
            next_run = self._next_run()
            wait = self._seconds_until(next_run)
            logger.info(f"[知识星球] 下次抓取: {next_run} (等待 {wait:.0f}s)")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                break  # 停止信号
            except asyncio.TimeoutError:
                hour = next_run.hour
                try:
                    result = await self._run_round_async(hour)
                    self._done[f"{next_run.date()}:{hour}"] = datetime.now()
                    # 清理 7 天前的状态
                    cutoff = (datetime.now() - timedelta(days=7)).date()
                    self._done = {k: v for k, v in self._done.items()
                                  if datetime.strptime(k.split(":")[0], "%Y-%m-%d").date() >= cutoff}
                    logger.info(f"[知识星球] {hour}:00 轮次完成")
                except Exception as e:
                    logger.error(f"[知识星球] {hour}:00 轮次失败: {e}")

    def start(self):
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop())
            logger.info("[知识星球] 研报抓取调度器已启动")

    async def stop(self):
        self._stop_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("[知识星球] 研报抓取调度器已停止")

    async def trigger_now(self, hour: Optional[int] = None) -> dict:
        """手动立即触发一轮"""
        h = hour if hour is not None else datetime.now().hour
        return await self._run_round_async(h)


# 单例
_scheduler: Optional[ZSXQScheduleScheduler] = None


def get_zsxq_scheduler() -> ZSXQScheduleScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ZSXQScheduleScheduler()
    return _scheduler
