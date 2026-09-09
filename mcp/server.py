# -*- encoding: utf-8 -*-
"""
stdio MCP 服务 (零依赖手写 JSON-RPC, 不装官方 SDK 也能跑)

启动 (项目根目录):
    .venv\\Scripts\\python.exe -m mcp.server

客户端 (ZCode/Claude Desktop 等) 配置 stdio 类型即可:
    command: <项目根>/.venv/Scripts/python.exe
    args:    ["-m", "mcp.server"]
    cwd:     <项目根>

支持方法: initialize / ping / tools/list / tools/call
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "alpha-finance-radar-mcp", "version": "1.0.0"}

# 服务器进程是长驻单线程: 注册表在启动时加载一次
from mcp.tools import REGISTRY  # noqa: E402

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
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result},
                      ensure_ascii=False, default=str)


def _rpc_error(req_id: Any, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id,
                       "error": {"code": code, "message": message}},
                      ensure_ascii=False)


def handle(msg: Dict[str, Any]) -> Optional[str]:
    """处理单条 JSON-RPC 消息; notification 返回 None (不回包)"""
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
        if READ_ONLY:
            spec = REGISTRY.get(name)
            if spec is not None and not spec.read_only:
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
