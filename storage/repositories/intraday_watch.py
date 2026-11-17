"""Atomic, isolated intraday report files; never replace official scan tables."""
from pathlib import Path
import re

from core.config import get_config
from core.local_state import read_json, write_json


def root():
    return Path(get_config().root_dir) / "output" / "market" / "intraday_watch"


def read_report():
    return read_json(root() / "latest.json", strict=True) or None


def publish_report(report):
    write_json(root() / "latest.json", report)


def chart_bytes(run_id, code):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}", run_id) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        raise ValueError("盘中报告标识不合法")
    path = root() / run_id / "charts" / (code + ".png")
    return path.read_bytes() if path.is_file() else None
