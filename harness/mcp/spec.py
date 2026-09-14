# -*- encoding: utf-8 -*-
"""
MCP 工具规范 (ToolSpec)
MCP Tool = name + description + JSON Schema + handler。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


class ToolError(Exception):
    """工具级可预期错误 (面向用户的中文消息)"""


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
        """MCP 协议 tools/list 条目"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.params_schema,
        }

    def to_openai(self) -> Dict[str, Any]:
        """OpenAI function-calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }


# ---------- 参数 Schema 构造小助手 (让工具定义少写样板) ----------

def obj_schema(
        properties: Dict[str, Any], required: List[str] = None
) -> Dict[str, Any]:
    """JSON Schema object 模板"""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def param(desc: str, ptype: str = "string", **extra) -> Dict[str, Any]:
    """单个参数 Schema: param"""
    return {"type": ptype, "description": desc, **extra}
