"""
每日增量数据同步调度器 (asyncio 后台任务, 后端常驻即调度常驻)

调度规则:
- 每交易日 18:00 触发 (Tushare 15:00-16:00 入库当日数据, 18:00 拉取稳妥)
- 增量: 最近 5 个交易日行情 (daily/daily_basic/adj_factor) + 当周财务
- 幂等: 重复执行安全 (断点续传 + upsert)

设计:
- asyncio task 挂在 FastAPI lifespan
- 纯标准库, 无外部依赖 (不用 APScheduler, 避免网络安装失败)
- 支持手动触发接口 /admin/sync-today
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from typing import Optional

from core.logger import get_logger
from tools.kline.calendar import is_trading_day

logger = get_logger(__name__)

# 调度时间: 每交易日 21:00 (Tushare 15-16点入库当日, 21:00 稳妥)
# 可通过 config.yaml 的 schedule.sync_hour 覆盖
from core.config import get_config as _cfg

SYNC_HOUR = int(_cfg().get("schedule.sync_hour", 21))
SYNC_MINUTE = int(_cfg().get("schedule.sync_minute", 0))
# 增量回溯天数 (交易日)
BACKFILL_DAYS = 5


class DailySyncScheduler:
    """每日增量同步调度器"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.last_run: Optional[datetime] = None
        self.last_result: Optional[dict] = None

    def _next_run_time(self, now: Optional[datetime] = None) -> datetime:
        """计算下次运行时间 (今天18:00 或 明天18:00)"""
        now = now or datetime.now()
        target = now.replace(hour=SYNC_HOUR, minute=SYNC_MINUTE,
                             second=0, microsecond=0)
        if now >= target:
            # 已过今天18:00 -> 明天
            target += timedelta(days=1)
        return target

    def _seconds_until(self, target: datetime) -> float:
        return max(0.0, (target - datetime.now()).total_seconds())

    async def _run_sync(self) -> dict:
        """执行一次增量同步"""
        logger.info("开始每日增量同步")
        from tools.market.sync_tushare import TusharePgSyncer

        end = date.today()
        # 回溯 N 个交易日
        start = end
        back = 0
        while back < BACKFILL_DAYS:
            start -= timedelta(days=1)
            if is_trading_day(start):
                back += 1

        syncer = TusharePgSyncer()
        try:
            # 单次 run 多表 (内部复用连接/白名单/日历)
            result = syncer.run(
                start=start, end=end,
                tables=["daily", "daily_basic", "adj_factor"],
            )
        except Exception as e:
            logger.error(f"增量同步异常: {e}")
            result = {"error": str(e)}

        self.last_run = datetime.now()
        self.last_result = result
        logger.info(f"每日增量同步完成: {result}")
        return result

    async def _loop(self):
        """调度循环"""
        logger.info(f"增量同步调度器已启动 (每日 {SYNC_HOUR}:{SYNC_MINUTE:02d})")
        while not self._stop_event.is_set():
            next_run = self._next_run_time()
            # 只在交易日跑
            if is_trading_day(next_run.date()):
                wait = self._seconds_until(next_run)
                logger.info(f"下次增量同步: {next_run} (等待 {wait:.0f}s)")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait
                    )
                    break  # 收到停止信号
                except asyncio.TimeoutError:
                    await self._run_sync()
            else:
                # 非交易日 -> 跳到明天
                wait = self._seconds_until(next_run)
                logger.debug(f"非交易日 {next_run.date()}, 等待 {wait:.0f}s")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait
                    )
                    break
                except asyncio.TimeoutError:
                    continue

    def start(self):
        """启动调度任务"""
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop())
            logger.info("每日增量调度器已启动")

    async def stop(self):
        """停止调度任务"""
        self._stop_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("每日增量调度器已停止")

    async def trigger_now(self) -> dict:
        """手动立即触发一次同步"""
        return await self._run_sync()


# 单例
_scheduler: Optional[DailySyncScheduler] = None


def get_scheduler() -> DailySyncScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DailySyncScheduler()
    return _scheduler
