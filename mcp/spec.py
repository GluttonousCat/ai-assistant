# -*- encoding: utf-8 -*-
"""
MCP 工具规范 (ToolSpec)

一个 MCP 工具 = name + 中文 description + JSON Schema 参数 + 同步 handler。
本文件只定义规范, 不含任何具体工具 (工具在 mcp/tools/ 按域分组实现)。

设计约定 (审核要点):
- name: 英文 snake_case, 全局唯一, 动词开头 (query_/get_/analyze_/run_...)
- description: 一律简体中文, 写清「做什么 + 什么时候该调我 + 不做什么」;
  LLM (deepseek-v4-flash) 靠它做工具路由, 中文描述选路最准
- params_schema: 标准 JSON Schema (type/properties/required/enum);
  参数名英文 snake_case, 参数 description 中文
- handler: 同步函数 (重 IO 由上层 asyncio.to_thread 包裹), 返回 JSON 可序列化 dict;
  面向用户的错误抛 ToolError (消息中文), 其他异常由 registry 兜底
- read_only: False 的工具 (写库/生成文件/调 LLM 生成) 需调用方确认
- examples: 中文示例问题, 不进 MCP 协议, 供人工审核与意图路由提示词使用
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


class ToolError(Exception):
    """工具级可预期错误 (面向用户的中文消息, 如「未识别到股票」)"""


@dataclass
class ToolSpec:
    """MCP 工具定义 (与 MCP 协议 tools/list 的 name/description/inputSchema 一一对应)"""

    name: str
    domain: str                              # entity/financial/report/chain/quant/risk
    description: str                         # 中文能力描述 (LLM 路由依据)
    params_schema: Dict[str, Any]            # JSON Schema object
    handler: Callable[..., Any]
    examples: List[str] = field(default_factory=list)
    read_only: bool = True
    notes: str = ""                          # 审核备注: 性能/限制/数据覆盖

    def to_mcp(self) -> Dict[str, Any]:
        """MCP 协议 tools/list 条目"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.params_schema,
        }

    def to_openai(self) -> Dict[str, Any]:
        """OpenAI function-calling 格式 (内部 Agent Loop / deepseek 用)"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }


# ---------- 参数 Schema 构造小助手 (让工具定义少写样板) ----------

def obj_schema(properties: Dict[str, Any], required: List[str] = None) -> Dict[str, Any]:
    """JSON Schema object 模板"""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def param(desc: str, ptype: str = "string", **extra) -> Dict[str, Any]:
    """单个参数 Schema: param('股票名或代码', 'string')"""
    return {"type": ptype, "description": desc, **extra}
