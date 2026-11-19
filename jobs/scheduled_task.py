"""Async task lifecycle shared by the intraday and email schedules."""
import asyncio
from abc import ABC, abstractmethod

from core.logger import get_logger

logger = get_logger(__name__)


class ScheduledTask(ABC):
    def __init__(self, name):
        self._name = name
        self._task = None
        self._stop = asyncio.Event()
        self.next_run = None
        self.last_result = None

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    async def _loop(self): ...

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    def start(self):
        if self.enabled and not self.running:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name=self._name)

    async def _wait(self, seconds):
        if self._stop.is_set():
            return True
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self):
        self._stop.set()
        try:
            if self.running:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        finally:
            self.next_run = None
        logger.info("%s调度已停止", self._name)
