# -*- encoding: utf-8 -*-
"""
mcp/ — MCP tool layer: standardized tooling of the platform's 22 research capabilities (19 read-only + 3 write).

- spec.py     ToolSpec convention (name/Chinese description/JSON Schema/handler)
- registry.py registry (decorator registration / unified call / dual-protocol export)
- tools/      22 tool implementations grouped into 6 domains (see tools/__init__.py comments)
- server.py   stdio MCP server (JSON-RPC, zero dependencies)
- bridge.py   OpenAI function-calling bridge (for the internal Agent Loop / deepseek)

Usage:
    from harness.mcp.tools import REGISTRY
    REGISTRY.call("query_financials", {"mode": "single",
                                       "stocks": ["贵州茅台"], "metrics": ["营收"]})
"""
from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolError, ToolSpec


def get_registry():
    """Get the registry with all tools registered (idempotent).

    Note: tool registration happens at import of harness.mcp.tools.
    A bare `from harness.mcp.registry import REGISTRY` yields an empty table (only for use at the definition site).
    """
    import harness.mcp.tools  # noqa: F401 importing performs registration
    return REGISTRY


__all__ = ["REGISTRY", "get_registry", "ToolSpec", "ToolError"]
