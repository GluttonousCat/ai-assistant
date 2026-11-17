"""Import the domain adapters once to register all MCP tools."""

from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolSpec
from harness.mcp.tools import (
    entity as entity,
    financial as financial,
    reports as reports,
    chain_alpha as chain_alpha,
    risk as risk,
    content as content,
)

__all__ = ["REGISTRY", "ToolSpec"]
