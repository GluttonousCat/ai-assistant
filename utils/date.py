# -*- encoding: utf-8 -*-
"""
@date: 2026/03/25
@file: date.py
@author: GluttonousCat
"""
from __future__ import annotations

import pandas as pd
from datetime import datetime, time, timedelta

import chinese_calendar as calendar

from core.logger import get_logger

logger = get_logger(__name__)


def skip_weekend(start_date_str: str, end_date_str: str) -> pd.DataFrame:
    """
    Generate a calendar range and distinguish between weekdays and weekends.
    :param start_date_str: Start date in string format (e.g., '2024-01-01').
    :param end_date_str: End date in string format.
    :return: A df containing the date, weekday number (0-6), and type label.
    """
    start_dt = pd.to_datetime(start_date_str)
    end_dt = pd.to_datetime(end_date_str)

    date_range = pd.date_range(start=start_dt, end=end_dt)

    logger.info(f"Checking {len(date_range)} calendar days")

    df = pd.DataFrame({'date': date_range})

    df['weekday_num'] = df['date'].dt.weekday
    df['is_weekend'] = df['date'].dt.weekday >= 5

    df['day_type'] = df['is_weekend'].apply(
        lambda x: '休息日' if x else '工作日')

    return df


def get_trading_days(start_date_str: str, end_date_str: str) -> pd.DataFrame:
    """
    Retrieve China A-share trading days within a given range.
    :param start_date_str: Start date in string format.
    :param end_date_str: End date in string format.
    :return: A DataFrame containing trading dates and corresponding weekdays.
    """
    start_dt = pd.to_datetime(start_date_str)
    end_dt = pd.to_datetime(end_date_str)

    date_range = pd.date_range(start=start_dt, end=end_dt)

    trading_dates = []

    for current_date in date_range:
        date_obj = current_date.to_pydatetime().date()

        is_weekday = current_date.weekday() < 5
        is_holiday = calendar.is_holiday(date_obj)

        if is_weekday and not is_holiday:
            trading_dates.append({
                'date': current_date,
                'weekday': current_date.weekday() + 1,
                'is_trading': True
            })

    return pd.DataFrame(trading_dates)


def is_trading_day(date_obj: datetime.date) -> bool:
    """Check if a specific date is a valid trading day in the China market."""
    return date_obj.weekday() < 5 and not calendar.is_holiday(date_obj)


def is_trading_time(timestamp_str: str) -> bool:
    """Determine if a timestamp falls within active market trading hours."""
    ts = pd.to_datetime(timestamp_str)
    date_part = ts.date()
    time_part = ts.time()

    if not is_trading_day(date_part):
        return False

    morning_start = time(9, 30, 0)
    morning_end = time(11, 30, 0)
    afternoon_start = time(13, 0, 0)
    afternoon_end = time(15, 0, 0)

    is_morning = morning_start <= time_part <= morning_end
    is_afternoon = afternoon_start <= time_part <= afternoon_end

    return is_morning or is_afternoon


def get_last_trading_day(
        ref_date: str | datetime | None = None) -> datetime.date:
    """
    Find the most recent trading day prior to the reference date.
    :param ref_date: The date to start searching from. Defaults to current time.
    :return: The nearest preceding trading day as a date object.
    """
    if ref_date is None:
        current_dt = datetime.now()
    else:
        current_dt = pd.to_datetime(ref_date)

    check_date = current_dt.date() - timedelta(days=1)

    # Search backwards for up to 15 days to cover long holidays.
    for _ in range(15):
        if is_trading_day(check_date):
            return check_date
        check_date -= timedelta(days=1)

    logger.error(f"Could not find last trading day for {ref_date}")
    return check_date


def get_offset_trading_day(base_date: str | datetime, offset: int) -> datetime:
    """
    Calculate the T+N or T-N trading day relative to a base date.
    :param base_date: The starting date in string or datetime format.
    :param offset: Number of trading days to shift.
    :return: The resulting trading day as a datetime object.
    """
    current_dt = pd.to_datetime(base_date)
    step = 1 if offset > 0 else -1
    count = 0

    while count < abs(offset):
        current_dt += timedelta(days=step)
        if is_trading_day(current_dt.date()):
            count += 1

    return current_dt


def get_period_ends(start_date: str, end_date: str, freq: str = 'ME') -> list:
    """
    Identify the last trading day of each period within a range.
    :param start_date: Start date in string format.
    :param end_date: End date in string format.
    :param freq: The frequency of the period ends.
    :return: A list of datetime objects representing the period ends.
    """
    all_days = pd.date_range(start_date, end_date)
    trading_days = [d for d in all_days if is_trading_day(d.date())]

    if not trading_days:
        return []

    # Create a DataFrame to leverage Pandas resampling capabilities
    df = pd.DataFrame(index=trading_days)
    df['date'] = df.index

    # Resample and take the last available trading day in each period
    resampled = df['date'].resample(freq).last()

    return resampled.dropna().tolist()
