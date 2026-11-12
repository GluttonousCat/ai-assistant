# -*- encoding: utf-8 -*-
"""
MCP ToolSpec.
An MCP Tool = name + description + JSON Schema + handler.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


class ToolError(Exception):
    """Tool-level expected error (user-facing Chinese message)."""


@dataclass
class ToolSpec:
    """MCP Tool"""

    name: str
    domain: str
    description: str
    params_schema: Dict[str, Any]
    handler: Callable[..., Any]
    examples: List[str] = field(default_factory=list)
    read_only: bool = True
    notes: str = ""

    def to_mcp(self) -> Dict[str, Any]:
        """MCP protocol tools/list entry."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.params_schema,
        }

    def to_openai(self) -> Dict[str, Any]:
        """OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }


# ---------- Parameter-schema builder helpers (less boilerplate in tool definitions) ----------

def obj_schema(
        properties: Dict[str, Any], required: List[str] = None
) -> Dict[str, Any]:
    """JSON Schema object template."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def param(desc: str, ptype: str = "string", **extra) -> Dict[str, Any]:
    """Single-parameter schema: param."""
    return {"type": ptype, "description": desc, **extra}
