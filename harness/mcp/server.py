# -*- encoding: utf-8 -*-
"""
Stdio MCP Server
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "alpha-finance-radar-mcp", "version": "1.0.0"}

from harness.mcp.tools import REGISTRY

# 沙盒: 默认只读模式 (仅暴露 read_only=True 工具, 写类如 forge_chain 不可见也不可调)
# 对外开放时保持默认; 需要放开生成类工具时设 ALPHA_MCP_READ_ONLY=0
READ_ONLY = os.environ.get("ALPHA_MCP_READ_ONLY", "1") not in ("0", "false", "False")


def _manifest() -> list:
    tools = REGISTRY.mcp_manifest()
    if READ_ONLY:
        tools = [t for t in tools
                 if REGISTRY.get(t["name"]) and REGISTRY.get(t["name"]).read_only]
    return tools


def _rpc_result(req_id: Any, result: Dict[str, Any]) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "result": result},
        ensure_ascii=False, default=str
    )


def _rpc_error(req_id: Any, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id,
                       "error": {"code": code, "message": message}},
                      ensure_ascii=False)


def handle(msg: Any) -> Optional[str]:
    """处理单条 JSON-RPC 消息; notification 返回 None (不回包)"""
    if not isinstance(msg, dict):
        # JSON-RPC 2.0: 非对象消息 (标量/数组) -> Invalid Request, id 置 null
        return _rpc_error(None, -32600,
                          "invalid request: message must be a JSON-RPC object")
    method = msg.get("method", "")
    req_id = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized" or is_notification:
        return None
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": _manifest()})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        spec = REGISTRY.get(name)
        # 协议级错误走 JSON-RPC error 而非 isError (MCP 规范): 未知工具 / arguments 非对象;
        # 业务失败仍走 isError, 中文原因留在 content 里供 LLM 自纠
        if spec is None:
            return _rpc_error(req_id, -32602, f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            return _rpc_error(req_id, -32602,
                              "Invalid params: 'arguments' must be an object")
        if READ_ONLY and not spec.read_only:
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(
                    {"ok": False, "tool": name,
                     "error": f"只读模式下已禁用写类工具: {name}"
                             f" (服务端设 ALPHA_MCP_READ_ONLY=0 可放开)"},
                    ensure_ascii=False)}],
                "isError": True,
            })
        # 底座模块 (如 range_trading 扫描器) 会 print 进度/排名;
        # stdio 协议下 stdout 必须只有 JSON-RPC, 全部重定向到 stderr
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):
            envelope = REGISTRY.call(name, arguments)
        text = json.dumps(envelope, ensure_ascii=False, default=str)
        # ok=False 用 isError 标记, 消息体仍在 content 里 (LLM 可读原因)
        return _rpc_result(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not envelope.get("ok", False),
        })
    if req_id is not None:
        return _rpc_error(req_id, -32601, f"method not found: {method}")
    return None


def main() -> None:
    # Windows 控制台缺省 GBK, 强制 UTF-8 读写 (MCP 传输层是 UTF-8)
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"[mcp-server] {len(_manifest())}/{len(REGISTRY.specs())} tools ready"
          f"{' (read-only)' if READ_ONLY else ''}",
          file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(_rpc_error(None, -32700, f"parse error: {e}") + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = handle(msg)
        except Exception as e:  # noqa: BLE001
            resp = _rpc_error(msg.get("id"), -32603, f"internal error: {e}")
        if resp is not None:
            sys.stdout.write(resp + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
