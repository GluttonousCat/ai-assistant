"""Shanghai billing window for work that can wait."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
OPEN = time(22)
CLOSE = time(8)
_background = ContextVar("offpeak_background", default=False)


def local_time(now=None):
    if now is None:
        return datetime.now(SHANGHAI)
    return now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)


def is_offpeak(now=None):
    clock = local_time(now).time()
    return clock >= OPEN or clock < CLOSE


def next_open(now=None):
    now = local_time(now)
    return datetime.combine(now.date() + timedelta(days=now.time() >= OPEN), OPEN, SHANGHAI)


class OffPeakEnded(RuntimeError):
    """The caller can retry in the next window without saving a failure."""

    def __init__(self):
        super().__init__("等待北京时间 22:00–08:00 闲时处理")


def require_offpeak():
    if not is_offpeak():
        raise OffPeakEnded()


def check_model_window():
    if _background.get():
        require_offpeak()
        return True
    return False


@contextmanager
def background_models():
    require_offpeak()
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)
