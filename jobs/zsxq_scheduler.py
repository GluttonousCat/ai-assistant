# -*- encoding: utf-8 -*-
"""
Zhixingqiu research-report daily crawl scheduler (asyncio background task; lives
with the backend process).

Schedule rules:
- One round at 12:00 and 24:00 each day
- Round flow:
    1. Crawl the latest topics (incremental; report core-view text lives in topics)
    2. Collect the file list (incremental) + download pending files (limited,
       anti-detection intervals)
    3. Sync to the PG report warehouse (fin.report_meta / report_forecast)
    4. Run LLM structured extraction on un-analyzed reports (limited, cost control)
- Idempotent: safe to re-run (topics/files deduplicated by ID, PG sync resumes from
  checkpoints, extraction gated by analysis_status)
- Startup catch-up: if the machine boots after 07:00 and the day's first round has
  not run (user powers on ~6:30; the server auto-starts at login), run one round
  immediately at startup so the platform has the latest reports in the morning
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional, Any, Dict

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)

# Schedule slots (overridable via config.yaml zsxq_schedule; hour domain 0-23,
# 24 is normalized to 0)
RUN_HOURS = [12, 0]
# Per-round LLM extraction cap (token cost control; the 23:00 round runs more)
EXTRACT_LIMITS = {7: 8, 23: 15}
# Per-round file-download cap
MAX_FETCH_FILES = 15
# Per-round LLM Analysis (lightweight metadata) cap — seconds per report, relaxed
META_ANALYSIS_LIMIT = 40


class ZSXQScheduleScheduler:
    """Zhixingqiu research-report daily crawl scheduler."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.last_run: Optional[datetime] = None
        self.last_result: Optional[dict] = None
        self._done: dict = {}

        cfg = get_config()
        hours = cfg.get("zsxq_schedule.run_hours")
        if isinstance(hours, list) and hours:
            # hour valid domain is 0-23: values like 24 are normalized to 0 (this
            # once made replace(hour=24) raise ValueError -> the scheduling
            # coroutine died silently, killing the startup catch-up and all
            # subsequent rounds)
            self.run_hours = sorted({int(h) % 24 for h in hours})
        else:
            self.run_hours = RUN_HOURS

    # ---------- Time computation ----------
    def _next_run(self, now: Optional[datetime] = None) -> datetime:
        now = now or datetime.now()
        for h in self.run_hours:
            target = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if now < target:
                return target
        return (now + timedelta(days=1)).replace(
            hour=self.run_hours[0], minute=0, second=0, microsecond=0)

    def _seconds_until(self, target: datetime) -> float:
        return max(0.0, (target - datetime.now()).total_seconds())

    # ---------- Round execution ----------
    def _run_round(self, hour: int) -> dict:
        """Run one crawl round synchronously (executed in the thread pool)."""
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

        # 1+2: crawl topics + collect/download files (reuses
        # daily_fetch_zxsq.run_once)
        try:
            from jobs.daily_fetch_zxsq import run_once
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

        # 3: LLM Analysis metadata extraction (org/target/industry/region/market
        #    -> table columns; lightweight, seconds per report)
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

        # 4: LLM deep extraction (rating/forecasts/views/tags)
        try:
            from skills.base import SkillContext
            from skills.report.skill import ReportSkill
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

        # 5: at the end of the morning round, generate the daily digest
        #    (output/daily_digest/YYYY-MM-DD.md; failure does not affect the main
        #    round)
        if hour == self.run_hours[0]:
            try:
                from jobs.daily_digest import generate
                result["digest"] = str(generate())
            except Exception as e:
                logger.error(f"每日研报晨报生成失败: {e}")
                result["digest"] = {"error": str(e)}

        self.last_run = datetime.now()
        self.last_result = result
        return result

    async def _run_round_async(self, hour: int) -> dict:
        """Async wrapper: heavy work goes to the thread pool, not blocking the
        event loop."""
        return await asyncio.to_thread(self._run_round, hour)

    # ---------- Startup catch-up ----------
    async def _catchup_if_missed(self):
        """
        Startup catch-up: user powers on ~6:30; the server auto-starts at login.
        If a scheduled slot has already passed today and that round has not run ->
        immediately run the most recently missed round.
        (The 23:00 round is normally triggered by the resident process and rarely
        missed; this mainly covers 07:00.)
        """
        now = datetime.now()
        today = now.date()
        # Find the latest round today whose slot has passed but which has not run
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

    # ---------- Scheduling loop ----------
    async def _loop(self):
        logger.info(f"[知识星球] 研报抓取调度器已启动 (每日 {'/'.join(f'{h:02d}:00' for h in self.run_hours)})")
        try:
            await self._catchup_if_missed()
        except Exception as e:
            # An exception in the catch-up phase must never kill the loop (an
            # invalid config once killed it silently, disabling all scheduling)
            logger.error(f"[知识星球] 启动补跑异常 (调度继续): {e}")

        while not self._stop_event.is_set():
            try:
                next_run = self._next_run()
                wait = self._seconds_until(next_run)
            except Exception as e:
                logger.error(f"[知识星球] 下轮时间计算失败, 1 小时后重试: {e}")
                await asyncio.sleep(3600)
                continue
            logger.info(f"[知识星球] 下次抓取: {next_run} (等待 {wait:.0f}s)")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                break  # stop signal
            except asyncio.TimeoutError:
                hour = next_run.hour
                try:
                    result = await self._run_round_async(hour)
                    self._done[f"{next_run.date()}:{hour}"] = datetime.now()
                    # Clean up state older than 7 days
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
        """Manually trigger one round immediately."""
        h = hour if hour is not None else datetime.now().hour
        return await self._run_round_async(h)


# Singleton
_scheduler: Optional[ZSXQScheduleScheduler] = None


def get_zsxq_scheduler() -> ZSXQScheduleScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ZSXQScheduleScheduler()
    return _scheduler
