"""Evaluate a frozen opportunity's support at completed exchange bar closes."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo


SESSION_CLOSES = ("10:30:00", "11:30:00", "14:00:00", "15:00:00")


def market_timestamp(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return stamp.isoformat(sep=" ")


def positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def bars_by_close(frame):
    return {(row["ts_code"], str(row["bar_end"]).replace("T", " ")[:19]): row
            for row in frame.to_dict("records")}


def support_exit(episode, calendar, cutoff, daily, minute):
    """Return the first two consecutive closes below the first published support."""
    code, first = episode["ts_code"], episode["first_seen"]
    price = positive(episode.get("stop_price"))
    factor = positive(daily.get((code, first + " 15:00:00"), {}).get("adj_factor"))
    if price is None or factor is None or first not in calendar:
        return None
    floor = price * factor
    published = market_timestamp(episode.get("first_published_at"))
    below_before = False
    for day in calendar:
        if day <= first:
            continue
        for clock in SESSION_CLOSES:
            stamp = day + " " + clock
            if stamp > cutoff:
                return None
            if published and stamp <= published:
                continue
            bar = minute.get((code, stamp), {})
            close, adjustment = positive(bar.get("close")), positive(bar.get("adj_factor"))
            traded = positive(bar.get("volume")) is not None
            below = bool(traded and close and adjustment and close * adjustment < floor
                         and not math.isclose(close * adjustment, floor, rel_tol=1e-9))
            if below and below_before:
                return stamp
            below_before = below
    return None
