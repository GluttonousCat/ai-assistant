# -*- encoding: utf-8 -*-
"""
OpenAI function-calling 桥: 内部 Agent Loop (deepseek, OpenAI 兼容接口) 直接消费

用法 (Agent Loop 内):
    from llm.client import get_agent_llm
    from mcp.bridge import openai_tools, run_tool_call

    tools = openai_tools()
    resp = llm.chat(messages, tools=tools, tool_choice="auto")   # LLMClient 扩展点
    for tc in resp.tool_calls:
        env = run_tool_call(tc)          # -> {"ok","tool","data"|"error"}
        messages.append({"role": "tool", "tool_call_id": tc.id,
                         "content": json.dumps(env, ensure_ascii=False)})

同一份 ToolSpec 同时服务 MCP 协议 (server.py) 与内部 function calling,
两边能力永不分叉。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from mcp.tools import REGISTRY


def openai_tools(include_write: bool = False) -> List[Dict[str, Any]]:
    """OpenAI tools 参数 (默认只给只读工具, 写类需显式放开)"""
    return REGISTRY.openai_manifest(include_write=include_write)


def run_tool_call(name: str, arguments_json: str = "{}") -> Dict[str, Any]:
    """执行一次 tool_call (参数是 LLM 给的 JSON 字符串)"""
    try:
        args = json.loads(arguments_json or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError as e:
        return {"ok": False, "tool": name, "error": f"参数 JSON 解析失败: {e}"}
    return REGISTRY.call(name, args)


def system_prompt_hint() -> str:
    """注入 Agent system prompt 的工具使用守则 (中文)"""
    return (
        "工具使用守则:\n"
        "1. 股票类工具直接传股票名/代码 (内部自解析); resolve_stock 只用于身份查询/"
        "多股票批量定位/别名歧义确认, 不作为其他工具的前置步骤\n"
        "2. 查数值用 query_financials; 问估值历史水位用 valuation_percentile\n"
        "3. 涉及研报先 search_reports, 需要 detail 再 read_report; 本地文件才 extract_document\n"
        "4. 预测看 get_forecasts; 历史兑现 verify_forecasts; 四象限综合 analyze_alpha\n"
        "5. 工具返回 ok=false 时把 error 原因告诉用户, 不要臆造数据\n"
        "6. 多工具结果由你综合成中文结论, 引用具体数值, 不做投资建议\n"
    )
