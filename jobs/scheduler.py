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
# 财报公告回溯天数 (日历天; 覆盖周末/节假日公告堆积)
FIN_BACKFILL_DAYS = 10


def _run_financial_incremental(days: int) -> dict:
    """线程池入口: 财报增量同步 (阻塞操作)"""
    from tools.market.sync_financial import run_financial_incremental
    return run_financial_incremental(days=days)


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

    async def _run_sync(self, include_financial: bool = True,
                        start: Optional[date] = None,
                        end: Optional[date] = None) -> dict:
        """执行一次增量同步 (行情 + 可选财报公告)"""
        logger.info("开始每日增量同步")
        from tools.market.sync_tushare import TusharePgSyncer

        end = end or date.today()
        # 回溯 N 个交易日 (或调用方指定的 start)
        if start is None:
            start = end
            back = 0
            while back < BACKFILL_DAYS:
                start -= timedelta(days=1)
                if is_trading_day(start):
                    back += 1

        syncer = TusharePgSyncer()
        try:
            # 单次 run 多表 (内部复用连接/白名单/日历); 阻塞 IO 放线程池
            result = await asyncio.to_thread(
                syncer.run, start, end,
                ["daily", "daily_basic", "adj_factor"],
            )
        except Exception as e:
            logger.error(f"增量同步异常: {e}")
            result = {"error": str(e)}

        # 财报增量: 拉取最近 FIN_BACKFILL_DAYS 天公告的财报 (放到线程池, 不阻塞事件循环)
        if include_financial:
            try:
                fin_result = await asyncio.to_thread(
                    _run_financial_incremental, FIN_BACKFILL_DAYS)
                if isinstance(result, dict):
                    result["financial"] = fin_result
            except Exception as e:
                logger.error(f"财报增量同步异常: {e}")
                if isinstance(result, dict):
                    result["financial"] = {"error": str(e)}

        self.last_run = datetime.now()
        self.last_result = result
        logger.info(f"每日增量同步完成: {result}")
        return result

    @staticmethod
    def _expected_latest_trade_date(now: Optional[datetime] = None) -> Optional[date]:
        """
        计算数据'应当已入库'的最新交易日:
        - 交易日 16:00 后 -> 今天 (Tushare 15-16 点入库当日数据)
        - 其他情况       -> 上一个交易日
        """
        now = now or datetime.now()
        d = now.date()
        if is_trading_day(d) and now.hour >= 16:
            return d
        d -= timedelta(days=1)
        for _ in range(20):  # 覆盖最长长假
            if is_trading_day(d):
                return d
            d -= timedelta(days=1)
        return None

    async def _catchup_if_missed(self):
        """
        启动自检补漏: 任意时间启动, 对比"应有最新交易日"与库内 daily 实际最新,
        存在缺口则立即补齐 (覆盖: 调度时段宕机 / 周末重启 / 长假后重启 /
        PG 不可达漏跑等全部场景). 只补行情 (分钟级); 财报留给 21:00 例行任务.
        """
        expected = self._expected_latest_trade_date()
        if expected is None:
            logger.warning("无法计算应有最新交易日, 跳过启动自检")
            return

        try:
            from storage.pg import PgClient
            with PgClient() as pg:
                row = pg.fetch_one("SELECT MAX(trade_date) m FROM stock.daily")
                latest = row["m"] if row else None
        except Exception as e:
            logger.error(f"启动自检查询失败 (PG 不可达?): {e}")
            return

        if latest is not None and latest >= expected:
            logger.info(f"启动自检: 行情已最新 (daily 至 {latest} >= 应有 {expected}), 无需补漏")
            return

        # 缺口起点: 库内最新次日; 无数据则回溯 10 个交易日
        if latest is not None:
            start = latest + timedelta(days=1)
        else:
            start = expected
            back = 0
            while back < 10:
                start -= timedelta(days=1)
                if is_trading_day(start):
                    back += 1

        logger.info(
            f"启动自检: 发现行情缺口 (库内最新 {latest}, 应有 {expected}), "
            f"立即补齐 {start} ~ {expected}"
        )
        try:
            await self._run_sync(include_financial=False,
                                 start=start, end=expected)
        except Exception as e:
            logger.error(f"启动补漏失败: {e}")

    async def _loop(self):
        """调度循环"""
        logger.info(f"增量同步调度器已启动 (每日 {SYNC_HOUR}:{SYNC_MINUTE:02d})")

        # 启动自检: 对比应有最新交易日与库内实际, 有缺口立即补齐 (任意时间/周末/长假后均生效)
        await self._catchup_if_missed()

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
