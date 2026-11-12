# -*- encoding: utf-8 -*-
"""
Agent full-chain tracing demo: complete inputs/outputs of every stage, from user question to final answer.

Chain: user question → intent routing (domain subagent / full toolset) → [LLM decision round <-> tool call]* → final answer

Recording (two hooks, zero intrusion into loop code):
  (1) Each LLM round: offered tool list / input message stream / response text + raw tool_calls
  (2) Each tool call: full input args and output envelope (incl. ok/elapsed/data preview)
Output: sectioned terminal printing + full JSON to output/agent_trace/
     (the SSE event stream is separately auto-persisted by harness.trajectory to logs/trajectories/)

Usage:
    .venv/Scripts/python.exe -m jobs.agent_trace_demo "中芯国际25年利润多少"
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402

get_config()  # Load .env (PG connection etc.)

RECORD: dict = {"question": None, "llm_rounds": [], "tool_calls": [], "answer": None}
OUT_DIR = ROOT / "output" / "agent_trace"
_PRINT_LOCK = threading.Lock()  # Parallel tool completions finish in nondeterministic order; lock to avoid interleaved printing


def _short(v, n: int = 600) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n}字符, 全文见JSON)"


def sec(title: str) -> None:
    with _PRINT_LOCK:
        print("\n" + "=" * 72 + f"\n== {title}\n" + "=" * 72, flush=True)


# ---------- Hook 1: LLM decision rounds (full request/response) ----------
import harness.llm as llm_mod

_real_get_agent_llm = llm_mod.get_agent_llm


class _TracingLLM:
    """Wrap LLMClient.invoke_with_tools, recording inputs/outputs round by round."""

    def __init__(self, inner):
        self._inner = inner
        self._round = 0

    def invoke_with_tools(self, messages, tools):
        self._round += 1
        rec = {"round": self._round,
               "tools_offered": [t["function"]["name"] for t in tools],
               "messages_in": [
                   {"role": m["role"],
                    "content": (m.get("content") or "")[:300],
                    **({"tool_calls": _short(m["tool_calls"], 400)}
                       if m.get("tool_calls") else {}),
                    **({"tool_call_id": m["tool_call_id"]}
                       if m.get("tool_call_id") else {})}
                   for m in messages[1:]]}  # [1:] skips the system prompt (full text in JSON)
        sec(f"LLM 决策轮 #{self._round} 请求")
        print(f"  可选工具 ({len(rec['tools_offered'])} 个): "
              f"{', '.join(rec['tools_offered'])}")
        for m in rec["messages_in"][-5:]:
            body = (m.get("content") or m.get("tool_calls") or "")[:200]
            print(f"  [{m['role']}] {body}")
        t0 = time.time()
        msg = self._inner.invoke_with_tools(messages, tools)
        rec["elapsed_ms"] = int((time.time() - t0) * 1000)
        rec["response"] = {
            "content": msg.content,
            "tool_calls": [{"name": tc.function.name,
                            "arguments": tc.function.arguments}
                           for tc in (getattr(msg, "tool_calls", None) or [])],
        }
        sec(f"LLM 决策轮 #{self._round} 响应 ({rec['elapsed_ms']}ms)")
        if rec["response"]["content"]:
            print(f"  [文本] {rec['response']['content'][:300]}")
        for tc in rec["response"]["tool_calls"]:
            print(f"  [调工具] {tc['name']}({tc['arguments'][:220]})")
        if not rec["response"]["tool_calls"] and not rec["response"]["content"]:
            print("  [空响应]")
        RECORD["llm_rounds"].append(rec)
        return msg


llm_mod.get_agent_llm = lambda **kw: _TracingLLM(_real_get_agent_llm(**kw))

# ---------- Hook 2: tool execution (full args/result) ----------
from harness.mcp.registry import MCPToolRegistry

_real_call = MCPToolRegistry.call


def _traced_call(self, name, arguments=None):
    t0 = time.time()
    envelope = _real_call(self, name, arguments)
    rec = {"tool": name, "args": arguments,
           "elapsed_ms": int((time.time() - t0) * 1000),
           "ok": envelope.get("ok"), "error": envelope.get("error"),
           "data": envelope.get("data")}
    RECORD["tool_calls"].append(rec)
    with _PRINT_LOCK:
        print(f"\n== 工具执行: {name} ({rec['elapsed_ms']}ms) → "
              f"{'OK' if rec['ok'] else 'FAIL: ' + str(rec['error'])[:120]}")
        print(f"  入参: {_short(arguments, 300)}")
        if rec["ok"]:
            print(f"  出参: {_short(rec['data'], 700)}")
    return envelope


MCPToolRegistry.call = _traced_call


def main() -> None:
    q = sys.argv[1] if len(sys.argv) > 1 else "中芯国际25年利润多少"
    RECORD["question"] = q
    from harness.loop import run_agent

    sec("① 用户输入")
    print(f"  问题: {q}")
    print("  模式: fast_lane=False (强制走完整 Agent 循环: 意图路由→域子Agent→工具)")

    t0 = time.time()
    res = run_agent(q, session_id=f"trace-{datetime.now():%H%M%S}",
                    fast_lane=False)
    RECORD["answer"] = res["answer"]
    RECORD["error"] = res.get("error")

    n_llm, n_tool = len(RECORD["llm_rounds"]), len(RECORD["tool_calls"])
    sec(f"② 最终回答 (全程 {time.time() - t0:.1f}s | LLM {n_llm} 轮 | 工具 {n_tool} 次)")
    print(res["answer"])
    if res.get("error"):
        print("ERROR:", res["error"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"trace_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(RECORD, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\n[全量记录] {out}")
    print("[SSE 事件轨迹] logs/trajectories/ (harness.trajectory 自动落盘)")


if __name__ == "__main__":
    main()
