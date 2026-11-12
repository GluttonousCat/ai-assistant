# -*- encoding: utf-8 -*-
"""
harness/mcp/ — MCP tool layer: standardized tooling of the platform's 33 research capabilities (30 read-only + 3 write).

- spec.py     ToolSpec convention (name/Chinese description/JSON Schema/handler)
- registry.py registry (decorator registration / unified call / dual-protocol export)
- tools/      33 tool implementations grouped into 5 domains (see tools/__init__.py comments)
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
    from core.config import get_config
    get_config()
    import harness.mcp.tools  # noqa: F401 importing performs registration
    return REGISTRY


__all__ = ["REGISTRY", "get_registry", "ToolSpec", "ToolError"]
