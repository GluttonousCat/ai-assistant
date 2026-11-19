"""Shanghai close-of-day refresh clock shared by jobs and read-only status."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import get_config
from tools.kline.calendar import is_trading_day

SHANGHAI = ZoneInfo("Asia/Shanghai")


def market_now() -> datetime:
    return datetime.now(SHANGHAI)


def market_time(now: datetime | None = None) -> datetime:
    if now is None:
        return market_now()
    return now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)


def expected_date(now=None, hour=None, minute=None):
    now = market_time(now)
    hour = int(get_config().get("schedule.sync_hour", 17)) if hour is None else hour
    minute = int(get_config().get("schedule.sync_minute", 0)) if minute is None else minute
    day = now.date()
    if is_trading_day(day) and (now.hour, now.minute) >= (hour, minute):
        return day
    for _ in range(20):
        day -= timedelta(days=1)
        if is_trading_day(day):
            return day
    return None
