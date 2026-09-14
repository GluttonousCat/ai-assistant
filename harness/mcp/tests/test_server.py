# -*- encoding: utf-8 -*-
"""mcp/server.py 协议行为单测 (无 IO/无 DB, 直接测 handle())

覆盖: 握手形状 / 错误通道分流 (协议级 JSON-RPC error vs 业务级 isError) /
通知不回包 / 只读清单过滤。stdio 端到端见 mcp/README.md 验证记录。
"""
from __future__ import annotations

import json

from harness.mcp import server


def _resp(s) -> dict:
    return json.loads(s)


def test_initialize_shape():
    r = _resp(server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}))
    assert r["result"]["protocolVersion"] == server.PROTOCOL_VERSION
    assert "tools" in r["result"]["capabilities"]
    assert r["result"]["serverInfo"]["name"]


def test_ping_returns_empty_result():
    r = _resp(server.handle({"jsonrpc": "2.0", "id": 6, "method": "ping"}))
    assert r["result"] == {}


def test_notification_never_responds():
    assert server.handle({"jsonrpc": "2.0",
                          "method": "notifications/initialized"}) is None


def test_unknown_method_returns_32601():
    r = _resp(server.handle({"jsonrpc": "2.0", "id": 5,
                             "method": "resources/list"}))
    assert r["error"]["code"] == -32601


def test_non_object_message_returns_32600():
    # 合法 JSON 但非对象 (标量/数组) -> Invalid Request, id=null
    for bad in (["batch"], "hello", 42):
        r = _resp(server.handle(bad))
        assert r["error"]["code"] == -32600
        assert r["id"] is None


def test_unknown_tool_returns_32602():
    # MCP 规范: 未知工具是协议级错误, 走 JSON-RPC error 而非 isError
    r = _resp(server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "no_such_tool",
                                        "arguments": {}}}))
    assert r["error"]["code"] == -32602
    assert "no_such_tool" in r["error"]["message"]


def test_non_object_arguments_returns_32602():
    r = _resp(server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "resolve_stock",
                                        "arguments": ["x"]}}))
    assert r["error"]["code"] == -32602


def test_write_tool_blocked_in_read_only_mode():
    # 已注册但被只读闸门禁用的写类工具: 保留 isError + 中文原因 (策略提示而非协议错误)
    if not server.READ_ONLY:
        return
    r = _resp(server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "forge_chain",
                                        "arguments": {"theme": "x"}}}))
    assert r["result"]["isError"] is True
    assert "只读模式" in r["result"]["content"][0]["text"]


def test_tools_list_hidden_write_tools_in_read_only_mode():
    r = _resp(server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}))
    names = [t["name"] for t in r["result"]["tools"]]
    assert "query_financials" in names
    if server.READ_ONLY:
        assert "forge_chain" not in names
        assert "write_article" not in names
