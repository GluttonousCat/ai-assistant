# -*- encoding: utf-8 -*-
"""
Agent 会话 (Session) — 同一会话的完整上下文, 进程内存

会话 = 两类上下文, 生命周期一致 (TTL 30 分钟惰性清理, 重启即失):
- 对话历史 (history): user/assistant 轮 + 工具调用摘要, 注入 LLM 用
  (轮数/字符双限截断; 原始工具 JSON 不存——省 token, 全量轨迹在
  core/agent/trajectory.py 落盘)
- 股票槽位 (slots): 最近 3 只股票, 支持"它呢/环比呢"省略主语的追问
  (快车道与 Text-to-SQL 均消费)

设计: 个人平台单进程, 不落库; 多轮注入保持 user/assistant 严格配对。
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
# 对话历史 (history)
# ============================================================

_MAX_TURNS = 12
_INJECT_TURNS = 6
_INJECT_MAX_CHARS = 6000

_LOCK = threading.Lock()
_STORE: Dict[str, Deque[Dict[str, Any]]] = {}


def _cleanup_locked(now: float) -> None:
    """惰性清理过期会话"""
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
    自行生成"[本轮工具调用]"清单输出给用户) — 改由 get_messages
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


# ============================================================
# 股票槽位 (slots) — 省略主语追问的短期记忆
# ============================================================

_SLOT_LOCK = threading.Lock()
_SLOTS: Dict[str, Dict] = {}


def get_session_stocks(session_id: Optional[str]) -> List[Tuple[str, str]]:
    """取该会话最近一轮的股票 [(ts_code, name), ...]"""
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
    """写入该会话的股票槽位 (空列表也写入, 表示本轮无股票 -> 追问链断开)"""
    if not session_id:
        return
    with _SLOT_LOCK:
        # 惰性清理过期会话
        now = time.time()
        for k in [k for k, v in _SLOTS.items() if now - v["updated_at"] > TTL_SECONDS]:
            _SLOTS.pop(k, None)
        _SLOTS[session_id] = {
            "stocks": [(c, n) for c, n in stocks][:3],
            "updated_at": now,
        }


def update_session_from_result(session_id: Optional[str], user_input: str,
                               data_rows) -> None:
    """从本轮查询结果提取股票写回会话上下文.
    本轮提取不到股票 (继承场景) 则保留原上下文不覆盖。"""
    if not session_id:
        return
    stocks: List[Tuple[str, str]] = []
    try:
        from tools.finance.stock_kb import get_stock_kb
        kb = get_stock_kb()
        # 优先: 结果行里的 ts_code (财务 SQL 有该列)
        for row in (data_rows or [])[:5]:
            if isinstance(row, dict) and row.get("ts_code"):
                code = row["ts_code"]
                if code not in [c for c, _ in stocks]:
                    stocks.append((code, kb.get_name(code) or code))
        # 兜底: 从用户输入词典匹配 (行情 SQL 无 ts_code 列)
        if not stocks:
            m = kb.match_stock(user_input)
            if m:
                stocks = [(m[0], kb.get_name(m[0]) or m[1])]
    except Exception:
        return
    if stocks:
        set_session_stocks(session_id, stocks[:3])
