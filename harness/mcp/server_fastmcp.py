# -*- encoding: utf-8 -*-
"""FastMCP 出口 (stdio 默认 / --http streamable HTTP) — legacy server.py 的企业化平替

legacy (harness/mcp/server.py, 零依赖手写 JSON-RPC) 保留为回退出口;
本出口依赖 fastmcp 4.x, 与其共存, 外部客户端换接入命令即可迁移。

用法:
    python -m harness.mcp.server_fastmcp                 # stdio (dsh/Claude Desktop)
    python -m harness.mcp.server_fastmcp --http          # streamable HTTP (默认 127.0.0.1:8765)

环境变量:
    ALPHA_MCP_READ_ONLY=0   放开写类工具 (默认 1=只读沙盒, 写类不注册)
    MCP_HTTP_TOKEN=<token>  HTTP 模式 Bearer 静态令牌鉴权 (不设=无鉴权, 仅建议本机回环;
                            企业化建议反代 JWT/OAuth 或替换 _StaticTokenVerifier)
    MCP_HTTP_HOST / MCP_HTTP_PORT  (默认 127.0.0.1 / 8765)
"""
from __future__ import annotations

import argparse
import os
import sys


def _token_auth():
    """MCP_HTTP_TOKEN 启用时返回静态令牌校验器; 未设置返回 None (无鉴权)"""
    expected = os.environ.get("MCP_HTTP_TOKEN", "")
    if not expected:
        return None

    from fastmcp.server.auth import AccessToken, TokenVerifier

    class _StaticTokenVerifier(TokenVerifier):
        async def verify_token(self, token: str):
            if token == expected:
                return AccessToken(token=token, client_id="local", scopes=[])
            return None

    return _StaticTokenVerifier()


def main() -> None:
    parser = argparse.ArgumentParser(description="FastMCP 出口 (stdio/http)")
    parser.add_argument("--http", action="store_true", help="streamable HTTP 传输 (默认 stdio)")
    parser.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MCP_HTTP_PORT", "8765")))
    args = parser.parse_args()

    try:
        from harness.mcp.fastmcp_export import build_fastmcp, read_only_mode
    except ImportError as e:
        print(f"[fastmcp-server] 缺少依赖 fastmcp ({e}); "
              f"回退 legacy: python -m harness.mcp.server", file=sys.stderr)
        sys.exit(2)

    if args.http:
        auth = _token_auth()
        mcp, n = build_fastmcp(read_only=read_only_mode(), auth=auth)
        print(f"[fastmcp-server] {n} tools ready, http://{args.host}:{args.port}/mcp"
              f"{' (bearer 鉴权)' if auth else ' (无鉴权, 仅建议本机)'}"
              f"{' (read-only)' if read_only_mode() else ''}",
              file=sys.stderr, flush=True)
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp, n = build_fastmcp(read_only=read_only_mode())
        print(f"[fastmcp-server] {n} tools ready (stdio"
              f"{', read-only' if read_only_mode() else ''})",
              file=sys.stderr, flush=True)
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
