"""Local job progress and an OS-held lock shared by the scheduler and CLI.

The lock is released by the OS on process exit; a leftover file is harmless.
Progress is observational, never a substitute for warehouse coverage checks.
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import get_config
from core.local_state import file_lock, read_json, write_json


def _root():
    return Path(get_config().root_dir) / "output" / "market" / "refresh"


def read_state():
    return read_json(_root() / "status.json")


def write_state(state):
    write_json(_root() / "status.json", {
        **state, "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    })


def refresh_lock():
    return file_lock(_root() / "refresh.lock")
