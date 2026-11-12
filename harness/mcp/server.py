# -*- encoding: utf-8 -*-
"""
Stdio MCP Server (JSON-RPC over stdin/stdout, zero dependencies).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "alpha-finance-radar-mcp", "version": "1.0.0"}

from harness.mcp.tools import REGISTRY

# Sandbox: read-only mode by default (only read_only=True tools are exposed; write tools
# like forge_chain are neither visible nor callable). Keep the default when exposed
# externally; set ALPHA_MCP_READ_ONLY=0 to unlock generation-type tools.
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
    """Handle one JSON-RPC message; notifications return None (no reply)."""
    if not isinstance(msg, dict):
        # JSON-RPC 2.0: non-object messages (scalars/arrays) -> Invalid Request, id set to null
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
        # Protocol-level errors use JSON-RPC error, not isError (MCP spec): unknown tool /
        # non-object arguments; business failures still use isError, with the Chinese reason
        # kept in content for LLM self-correction
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
        # Foundation modules (scanners, report pipelines) print progress/rankings;
        # under the stdio protocol stdout must carry JSON-RPC only, so redirect all of it to stderr
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):
            envelope = REGISTRY.call(name, arguments)
        text = json.dumps(envelope, ensure_ascii=False, default=str)
        # ok=False is flagged via isError; the message body stays in content (reason readable by the LLM)
        return _rpc_result(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not envelope.get("ok", False),
        })
    if req_id is not None:
        return _rpc_error(req_id, -32601, f"method not found: {method}")
    return None


def main() -> None:
    # Windows console defaults to GBK; force UTF-8 for stdin/stdout (the MCP transport is UTF-8)
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
