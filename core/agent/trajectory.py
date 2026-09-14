# -*- encoding: utf-8 -*-
"""
Agent 轨迹日志 (append-only JSONL)

DSH "全程可追溯" 思想的低成本落地: 每轮对话 (run_agent_stream 一次调用)
落一个文件 logs/trajectories/<session>_<时间戳>.jsonl —
首行 turn_start (用户输入), 其后逐事件 tee SSE 流 (stage/tool_call/data/delta/
error), 事后可 replay 会话 / 归因 (evals 分析"模型为何误选工具"), 不再只有摘要
(session 为省 token 只存摘要, 原始工具 JSON 此前直接丢弃)。

- 零侵入: run_agent_stream 出口包装 sse formatter, 业务代码无感
- 开关/保留: config.yaml agent.trajectory.enabled / retention_days (0=不清理)
- 失败静默: 轨迹是旁路, 写入异常绝不影响对话主链路
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
    """惰性清理过期轨迹 (每小时最多扫一次; 持锁调用)"""
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
    """开一轮轨迹文件并写入 turn_start; 返回 (event, data)->None 的 tee 函数。

    agent.trajectory.enabled=false / 写失败 -> 返回 no-op (对话不受影响)。
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
