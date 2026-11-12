# -*- encoding: utf-8 -*-
"""
In-process metrics registry (data source for the Agent live monitor)

- Pure in-memory dicts/counters + threading.Lock; counters reset when the API
  service restarts, so the dashboard scope = "since service start" (uptime is
  sent alongside). No persistence, no external dependencies.
- Ingestion: harness/loop.py (sessions/funnel/rounds/tools), harness/llm.py
  (LLM calls/latency/tokens); record() swallows all exceptions — monitoring
  must never affect the main path.
- Egress: api/monitor_api.py pushes snapshot() to the frontend over SSE every 2s.
- Metrics are keyed by local date and roll over to zero at midnight (today scope).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date
from typing import Any, Dict

_LOCK = threading.Lock()
_STARTED_AT = time.time()

_day: str = date.today().isoformat()
_counters: Dict[str, int] = {}                 # generic counters (sessions/tool_calls/llm_fails...)
_funnel: Dict[str, int] = {"fast": 0, "subagent": 0, "full": 0}
_tools: Dict[str, Dict[str, int]] = {}         # {tool: {calls, fails, ms}}
_llm: Dict[str, Dict[str, int]] = {}           # {purpose: {calls, fails, ms, pt, ct}}
_sessions: Dict[str, Dict[str, Any]] = {}      # {sid: {start,text,path,rounds,tools,status,updated}}
_events: deque = deque(maxlen=150)             # monitor page event stream (ring buffer)

# Event types that enter the event stream (all others only hit counters/session table)
_STREAMED = {"session_start", "session_end", "tool", "llm_fail", "error"}

_SESSION_IDLE_S = 1800  # active-session window (evicted after 30min without updates)


def _rollover() -> None:
    global _day
    today = date.today().isoformat()
    if _day != today:
        _day = today
        for d in (_counters, _funnel, _tools, _llm):
            d.clear()


def record(event: str, **data: Any) -> None:
    """Unified instrumentation entry; any exception is swallowed (a monitoring failure must never leak into business)"""
    try:
        with _LOCK:
            _rollover()
            if event in _STREAMED:
                _events.append({"ts": time.time(), "event": event, **data})

            if event == "session_start":
                sid = str(data.get("session_id") or "-")
                _sessions[sid] = {
                    "start": time.time(), "text": str(data.get("text") or "")[:60],
                    "path": data.get("path") or "full", "rounds": 0,
                    "tools": 0, "status": "running", "updated": time.time(),
                }
                _counters["sessions"] = _counters.get("sessions", 0) + 1
                p = data.get("path") or "full"
                _funnel[p] = _funnel.get(p, 0) + 1

            elif event == "session_round":
                s = _sessions.get(str(data.get("session_id") or "-"))
                if s:
                    s["rounds"] = max(s.get("rounds", 0), int(data.get("round_no") or 0))
                    s["updated"] = time.time()

            elif event == "tool":
                t = str(data.get("tool") or "?")
                ms = int(data.get("ms") or 0)
                ok = bool(data.get("ok", True))
                st = _tools.setdefault(t, {"calls": 0, "fails": 0, "ms": 0})
                st["calls"] += 1
                st["fails"] += 0 if ok else 1
                st["ms"] += ms
                _counters["tool_calls"] = _counters.get("tool_calls", 0) + 1
                if not ok:
                    _counters["tool_fails"] = _counters.get("tool_fails", 0) + 1
                s = _sessions.get(str(data.get("session_id") or "-"))
                if s:
                    s["tools"] += 1
                    s["updated"] = time.time()

            elif event == "session_end":
                s = _sessions.get(str(data.get("session_id") or "-"))
                if s:
                    s["status"] = "done" if data.get("ok", True) else "error"
                    s["duration_s"] = round(time.time() - s["start"], 1)
                    s["updated"] = time.time()
                _counters["session_ends"] = _counters.get("session_ends", 0) + 1

            elif event == "llm":
                p = str(data.get("purpose") or "default")
                ms = int(data.get("ms") or 0)
                ok = bool(data.get("ok", True))
                st = _llm.setdefault(p, {"calls": 0, "fails": 0, "ms": 0, "pt": 0, "ct": 0})
                st["calls"] += 1
                st["fails"] += 0 if ok else 1
                st["ms"] += ms
                st["pt"] += int(data.get("pt") or 0)
                st["ct"] += int(data.get("ct") or 0)
                _counters["llm_calls"] = _counters.get("llm_calls", 0) + 1
                if not ok:
                    _counters["llm_fails"] = _counters.get("llm_fails", 0) + 1

            elif event == "escalate":
                _counters["escalates"] = _counters.get("escalates", 0) + 1

            elif event == "error":
                _counters["errors"] = _counters.get("errors", 0) + 1
    except Exception:  # noqa: BLE001
        pass


def snapshot() -> Dict[str, Any]:
    """Full state (polled by monitor SSE every 2s; values are shallow copies)"""
    with _LOCK:
        _rollover()
        now = time.time()
        for sid in [k for k, v in _sessions.items()
                    if now - v.get("updated", 0) > _SESSION_IDLE_S]:
            _sessions.pop(sid, None)
        return {
            "started_at": _STARTED_AT,
            "uptime_s": int(now - _STARTED_AT),
            "day": _day,
            "counters": dict(_counters),
            "funnel": dict(_funnel),
            "tools": {k: dict(v) for k, v in _tools.items()},
            "llm": {k: dict(v) for k, v in _llm.items()},
            "sessions": sorted(_sessions.values(),
                               key=lambda s: -s.get("updated", 0)),
            "events": list(_events)[-60:],
        }
