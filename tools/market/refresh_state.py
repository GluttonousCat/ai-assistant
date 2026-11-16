"""Local job progress and an OS-held lock shared by the scheduler and CLI.

The lock is released by the OS on process exit; a leftover file is harmless.
Progress is observational, never a substitute for warehouse coverage checks.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import get_config


def _root():
    return Path(get_config().root_dir) / "output" / "market" / "refresh"


def read_state():
    try:
        return json.loads((_root() / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state):
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    payload = {**state, "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
    tmp = root / f"status.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, root / "status.json")


@contextmanager
def refresh_lock():
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "refresh.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
