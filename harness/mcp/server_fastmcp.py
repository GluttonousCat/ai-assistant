# -*- encoding: utf-8 -*-
"""FastMCP exit (stdio by default / --http for streamable HTTP) — enterprise-grade replacement for legacy server.py.

The legacy exit (harness/mcp/server.py, zero-dependency hand-written JSON-RPC) is kept
as the fallback; this exit depends on fastmcp 4.x and coexists with it — external
clients migrate by switching their connect command.

Usage:
    python -m harness.mcp.server_fastmcp                 # stdio (dsh/Claude Desktop)
    python -m harness.mcp.server_fastmcp --http          # streamable HTTP (default 127.0.0.1:8765)

Environment variables:
    ALPHA_MCP_READ_ONLY=0   unlock write tools (default 1 = read-only sandbox; write tools not registered)
    MCP_HTTP_TOKEN=<token>  Bearer static-token auth in HTTP mode (unset = no auth; recommended only
                            for loopback. For enterprise use, front with a reverse proxy doing
                            JWT/OAuth, or replace _StaticTokenVerifier)
    MCP_HTTP_HOST / MCP_HTTP_PORT  (default 127.0.0.1 / 8765)
"""
from __future__ import annotations

import argparse
import os
import sys


def _token_auth():
    """Return a static-token verifier when MCP_HTTP_TOKEN is set; None (no auth) when unset."""
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
