# -*- encoding: utf-8 -*-
"""
Agent session — full context of one conversation, held in process memory.

A session = two context stores with a shared lifecycle (TTL 30 min with lazy cleanup, lost on restart):
- Conversation history: user/assistant turns + tool-call digests, injected into the LLM
  (dual turn-count/char limits; raw tool JSON is not kept — saves tokens; the full
  trajectory is persisted by harness/trajectory.py)
- Stock slots: the most recent 3 stocks, supporting subject-omitted follow-ups like
  "它呢/环比呢" (consumed by both the fast lane and Text-to-SQL)

Design: single-process personal platform, no persistence; multi-turn injection keeps
user/assistant strictly paired.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from core.logger import get_logger

logger = get_logger(__name__)

TTL_SECONDS = 30 * 60


# ============================================================
# Conversation history
# ============================================================

_MAX_TURNS = 12
_INJECT_TURNS = 6
_INJECT_MAX_CHARS = 6000

_LOCK = threading.Lock()
_STORE: Dict[str, Deque[Dict[str, Any]]] = {}


def _cleanup_locked(now: float) -> None:
    """Lazily clean up expired sessions."""
    expired = [sid for sid, turns in _STORE.items()
               if not turns or now - turns[-1]["ts"] > TTL_SECONDS]
    for sid in expired:
        del _STORE[sid]


def append_user(session_id: Optional[str], text: str) -> None:
    if not session_id:
        return
    with _LOCK:
        _cleanup_locked(time.time())
        turns = _STORE.setdefault(session_id, deque(maxlen=_MAX_TURNS))
        turns.append({"role": "user", "content": text,
                      "tools": [], "ts": time.time()})


def append_assistant(session_id: Optional[str], text: str,
                     tool_events: Optional[List[Dict[str, Any]]] = None) -> None:
    """Assistant turn; tool_events: [{name, args, ok, elapsed_ms, rows}]."""
    if not session_id:
        return
    with _LOCK:
        turns = _STORE.get(session_id)
        if not turns:
            return  # no matching user turn (error path); discard
        turns.append({"role": "assistant", "content": text,
                      "tools": tool_events or [], "ts": time.time()})


def _fmt_tool_line(t: Dict[str, Any]) -> str:
    arg_str = ",".join(f"{k}={v}" for k, v in (t.get("args") or {}).items()
                       if v not in (None, "", []))[:80]
    mark = "✓" if t.get("ok") else "✗"
    return f"{mark} {t['name']}({arg_str}) {t.get('rows', 0)}行"


def _turn_to_messages(turn: Dict[str, Any]) -> List[Dict[str, str]]:
    """Single turn -> LLM messages.

    Tool digests **never go into the assistant body** (when they were once concatenated
    into it, the model imitated them and emitted a "[本轮工具调用]" list at the end of
    answers to users) — instead get_messages injects them uniformly as a system role.
    """
    if turn["role"] != "assistant":
        return [{"role": "user", "content": turn["content"]}]
    content = turn.get("content") or ""
    if not content:
        return []
    return [{"role": "assistant", "content": content}]


def get_messages(session_id: Optional[str],
                 max_turns: int = _INJECT_TURNS,
                 max_chars: int = _INJECT_MAX_CHARS) -> List[Dict[str, str]]:
    """Recent session turns -> LLM messages.

    Pairs full turns (user + its assistant), takes the most recent max_turns turns, then
    truncates by max_chars; user/assistant always alternate starting with user (older turns
    cut in the middle are dropped whole — no orphan messages are produced).
    If any tool calls fall inside the injection window, a system-role background record is
    appended (so the model can resolve what a follow-up like "环比呢" refers to — which
    stock, which metric) — kept out of the assistant body to stop the model imitating it
    back to the user.
    """
    if not session_id:
        return []
    with _LOCK:
        _cleanup_locked(time.time())
        turns = list(_STORE.get(session_id) or [])
    if not turns:
        return []

    # Build full turns: a user turn plus the first assistant right after it (orphan assistants skipped)
    pairs: List[List[Dict[str, Any]]] = []
    i = 0
    while i < len(turns):
        if turns[i]["role"] == "user":
            if i + 1 < len(turns) and turns[i + 1]["role"] == "assistant":
                pairs.append([turns[i], turns[i + 1]])
                i += 2
            else:
                pairs.append([turns[i], None])
                i += 1
        else:
            i += 1

    msgs: List[Dict[str, str]] = []
    tool_lines: List[str] = []
    used = 0
    for u, a in pairs[-max_turns:]:
        amsgs = _turn_to_messages(a) if a else []
        seg_chars = len(u["content"]) + sum(len(m["content"]) for m in amsgs)
        if msgs and used + seg_chars > max_chars:
            break
        msgs.append({"role": "user", "content": u["content"]})
        msgs.extend(amsgs)
        used += seg_chars
        if a and a.get("tools"):
            head = u["content"][:16].replace("\n", " ")
            lines = "; ".join(_fmt_tool_line(t) for t in a["tools"][-6:])
            tool_lines.append(f"- 「{head}…」一轮: {lines}")

    if tool_lines:
        msgs.append({
            "role": "system",
            "content": "【后台工具记录 · 仅供你理解上文追问的所指对象 (股票/指标/链条), "
                       "严禁在回答中输出、复述或模仿此记录】\n" + "\n".join(tool_lines),
        })
    return msgs


def reset(session_id: Optional[str]) -> None:
    if not session_id:
        return
    with _LOCK:
        _STORE.pop(session_id, None)


# ============================================================
# Stock slots — short-term memory for subject-omitted follow-ups
# ============================================================

_SLOT_LOCK = threading.Lock()
_SLOTS: Dict[str, Dict] = {}


def get_session_stocks(session_id: Optional[str]) -> List[Tuple[str, str]]:
    """Get the session's most recent stocks [(ts_code, name), ...]."""
    if not session_id:
        return []
    with _SLOT_LOCK:
        entry = _SLOTS.get(session_id)
        if not entry:
            return []
        if time.time() - entry["updated_at"] > TTL_SECONDS:
            _SLOTS.pop(session_id, None)
            return []
        return list(entry["stocks"])


def set_session_stocks(session_id: Optional[str],
                       stocks: List[Tuple[str, str]]) -> None:
    """Write the session's stock slots (an empty list is also written, meaning no stock this turn -> follow-up chain breaks)."""
    if not session_id:
        return
    with _SLOT_LOCK:
        # Lazily clean up expired sessions
        now = time.time()
        for k in [k for k, v in _SLOTS.items() if now - v["updated_at"] > TTL_SECONDS]:
            _SLOTS.pop(k, None)
        _SLOTS[session_id] = {
            "stocks": [(c, n) for c, n in stocks][:3],
            "updated_at": now,
        }


def update_session_from_result(session_id: Optional[str], user_input: str,
                               data_rows) -> None:
    """Extract stocks from this turn's query result and write them back into session context.
    If no stock is extracted this turn (inheritance scenario), keep the existing context unoverwritten."""
    if not session_id:
        return
    stocks: List[Tuple[str, str]] = []
    try:
        from tools.finance.stock_kb import get_stock_kb
        kb = get_stock_kb()
        # Preferred: ts_code in result rows (financial SQL has that column)
        for row in (data_rows or [])[:5]:
            if isinstance(row, dict) and row.get("ts_code"):
                code = row["ts_code"]
                if code not in [c for c, _ in stocks]:
                    stocks.append((code, kb.get_name(code) or code))
        # Fallback: dictionary match against the user input (quotes SQL has no ts_code column)
        if not stocks:
            m = kb.match_stock(user_input)
            if m:
                stocks = [(m[0], kb.get_name(m[0]) or m[1])]
    except Exception:
        return
    if stocks:
        set_session_stocks(session_id, stocks[:3])
