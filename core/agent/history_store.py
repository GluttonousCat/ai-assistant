# -*- encoding: utf-8 -*-
"""
Agent 会话历史存储 (进程内存, 与 context_store 同生命周期策略)

设计:
- 每轮存 (user 文本 / assistant 回答 / 本轮工具调用摘要), 不存原始工具 JSON
  (原始结果太大, 摘要足够支撑追问: "环比下降对吗" 需要知道上轮查了谁+什么指标)
- 注入 LLM 时按 (轮数, 字符数) 双限截断, 只保留最近几轮
- TTL 30 分钟惰性清理 (个人平台单进程, 不落库; 重启即失, 与旧 context_store 一致)
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from core.logger import get_logger

logger = get_logger(__name__)

_TTL_SECONDS = 30 * 60
_MAX_TURNS = 12          # 单会话最多保留轮数 (内存保护)
_INJECT_TURNS = 6        # 注入 LLM 的最近轮数
_INJECT_MAX_CHARS = 6000

_LOCK = threading.Lock()
_STORE: Dict[str, Deque[Dict[str, Any]]] = {}


def _cleanup_locked(now: float) -> None:
    """惰性清理过期会话 (持锁调用)"""
    expired = [sid for sid, turns in _STORE.items()
               if not turns or now - turns[-1]["ts"] > _TTL_SECONDS]
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
    """assistant 轮; tool_events: [{name, args, ok, elapsed_ms, rows}]"""
    if not session_id:
        return
    with _LOCK:
        turns = _STORE.get(session_id)
        if not turns:
            return  # 无对应 user 轮 (异常路径), 丢弃
        turns.append({"role": "assistant", "content": text,
                      "tools": tool_events or [], "ts": time.time()})


def _fmt_tool_line(t: Dict[str, Any]) -> str:
    arg_str = ",".join(f"{k}={v}" for k, v in (t.get("args") or {}).items()
                       if v not in (None, "", []))[:80]
    mark = "✓" if t.get("ok") else "✗"
    return f"{mark} {t['name']}({arg_str}) {t.get('rows', 0)}行"


def _turn_to_messages(turn: Dict[str, Any]) -> List[Dict[str, str]]:
    """单轮 -> LLM messages。

    工具摘要**不进 assistant 正文** (曾因拼进正文被模型模仿, 在回答末尾
    自行生成"[本轮工具调用]"清单输出给用户) — 改由 _tool_record_message
    以 system 角色统一注入。
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
    """会话最近轮 -> LLM messages

    按完整轮 (user + 其 assistant) 配对, 取最近 max_turns 轮, 再受 max_chars 截断;
    始终保持 user/assistant 交替且以 user 开头 (中间被截的旧轮整体丢弃, 不产生孤儿消息)。
    末尾若注入窗口内有工具调用, 追加一条 system 角色的后台记录 (供模型解析追问所指,
    如"环比呢"该指向哪只股票哪个指标) — 不放 assistant 正文, 防模型模仿输出给用户。
    """
    if not session_id:
        return []
    with _LOCK:
        _cleanup_locked(time.time())
        turns = list(_STORE.get(session_id) or [])
    if not turns:
        return []

    # 组完整轮: user 开头, 紧随的第一条 assistant 归它 (孤儿 assistant 跳过)
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
