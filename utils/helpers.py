#!/usr/bin/env python3
"""
通用工具函数
"""
import re
from datetime import datetime, timedelta, timezone


def clean_cookie(cookie: str) -> str:
    """清理Cookie字符串"""
    if not cookie:
        return ""

    # 去除首尾空白和引号
    cookie = cookie.strip().strip('"\'')

    # 处理bytes类型
    if isinstance(cookie, bytes):
        cookie = cookie.decode('utf-8')

    # 去除前缀b'或b"
    if cookie.startswith("b'") and cookie.endswith("'"):
        cookie = cookie[2:-1]
    elif cookie.startswith('b"') and cookie.endswith('"'):
        cookie = cookie[2:-1]

    # 处理转义字符
    cookie = cookie.replace('\\n', '').replace('\\"', '"').replace("\\'", "'")

    # 确保分号后有空格
    cookie = '; '.join(part.strip() for part in cookie.split(';'))

    return cookie


def timestamp_to_iso(timestamp_ms: int) -> str:
    """毫秒时间戳转ISO格式"""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone(timedelta(hours=8)))
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0800'


def iso_to_timestamp(iso_time: str) -> int:
    """ISO格式转毫秒时间戳"""
    dt = datetime.fromisoformat(iso_time.replace('+0800', '+08:00'))
    return int(dt.timestamp() * 1000)


def decrement_time(iso_time: str, milliseconds: int = 1) -> str:
    """ISO时间减去指定毫秒"""
    dt = datetime.fromisoformat(iso_time.replace('+0800', '+08:00'))
    dt = dt - timedelta(milliseconds=milliseconds)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0800'


def sanitize_filename(filename: str) -> str:
    """清理文件名，移除非法字符"""
    safe = "".join(c for c in filename if c.isalnum() or c in '._-（）()[]{} ')
    return safe or "unnamed"
