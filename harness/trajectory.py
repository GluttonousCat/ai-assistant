# -*- encoding: utf-8 -*-
"""
Agent trajectory log (append-only JSONL).

A low-cost landing of the DSH "fully traceable" idea: each conversation turn (one
run_agent_stream call) writes one file logs/trajectories/<session>_<timestamp>.jsonl —
first line turn_start (user input), then every event tee'd from the SSE stream
(stage/tool_call/data/delta/error). Afterwards a session can be replayed / attributed
(evals can analyze "why the model picked the wrong tool") instead of having only
summaries (session stores digests only to save tokens; raw tool JSON used to be
discarded outright).

- Zero intrusion: run_agent_stream wraps its sse formatter at the exit; business code is unaware
- Switch/retention: config.yaml agent.trajectory.enabled / retention_days (0 = never clean up)
- Silent failure: trajectory is a side channel; write errors must never affect the main conversation chain
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)

_LOCK = threading.Lock()
_last_cleanup = 0.0


def _dir() -> Path:
    return Path(get_config().root_dir) / "logs" / "trajectories"


def _cleanup_locked(now: float) -> None:
    """Lazily clean up expired trajectories (scan at most once per hour; called with lock held)."""
    global _last_cleanup
    if now - _last_cleanup < 3600:
        return
    _last_cleanup = now
    try:
        days = float(get_config().get("agent.trajectory.retention_days", 30) or 0)
    except (TypeError, ValueError):
        days = 0.0
    if days <= 0:
        return
    cutoff = now - days * 86400
    for f in _dir().glob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _noop(event: str, data: Any) -> None:
    pass


def open_turn(session_id: Optional[str], text: str) -> Callable[[str, Any], None]:
    """Open one turn's trajectory file, write turn_start; returns a (event, data)->None tee function.

    agent.trajectory.enabled=false / write failure -> returns a no-op (conversation unaffected).
    """
    try:
        if not get_config().get("agent.trajectory.enabled", True):
            return _noop
        sid = (session_id or "cli").replace("/", "_").replace("\\", "_")[:48]
        path = _dir() / f"{sid}_{datetime.now():%Y%m%d_%H%M%S_%f}.jsonl"
        _dir().mkdir(parents=True, exist_ok=True)
        with _LOCK:
            _cleanup_locked(time.time())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"轨迹日志不可用 (本轮不记录): {e}")
        return _noop

    def _write(rec: dict) -> None:
        rec["ts"] = datetime.now().isoformat(timespec="milliseconds")
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    try:
        _write({"event": "turn_start", "session": sid, "text": text})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"轨迹日志写入失败 (本轮不记录): {e}")
        return _noop

    def tee(event: str, data: Any) -> None:
        try:
            _write({"event": event, "data": data})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"轨迹写入失败: {e}")

    return tee
