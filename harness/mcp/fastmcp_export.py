# -*- encoding: utf-8 -*-
"""REGISTRY -> FastMCP 适配层 (企业出口: stdio + streamable HTTP 的形态转换)

- 工具层零改动: ToolSpec (harness/mcp/spec.py) 仍是唯一事实源, 双 manifest
  (to_mcp/to_openai) 与本适配器三形态并存;
- 中文参数 schema 保留: Tool.from_function 从签名推骨架 -> model_copy 覆盖为
  手写 params_schema (含中文参数描述, LLM 选参质量的关键);
- 执行仍走 REGISTRY.call 统一信封 (计时/幻觉参数剔除/中文错误兜底);
  ok=False 抛 fastmcp ToolError -> 协议 isError (与 legacy server.py 行为等价);
- read_only 沙盒: 只读模式仅注册 read_only 工具 (ALPHA_MCP_READ_ONLY 同源约定);
- 同步 handler + run_in_thread=True: fastmcp 线程池执行, 无需异步化改造
  (配套: storage.pg PgPool 已线程安全化)。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Tuple

from harness.mcp.spec import ToolSpec
from harness.mcp.tools import REGISTRY


def read_only_mode() -> bool:
    """只读沙盒开关 (与 legacy server.py 同源: ALPHA_MCP_READ_ONLY, 默认开)"""
    return os.environ.get("ALPHA_MCP_READ_ONLY", "1") not in ("0", "false", "False")


def _make_handler(spec: ToolSpec) -> Callable[..., Dict[str, Any]]:
    """包一层: 统一信封 + ok=False 转 ToolError (isError 语义)。

    functools.wraps(handler) 让 from_function 经 __wrapped__ 解析到工具的
    显式参数签名 (fastmcp 不支持 **kwargs 签名); 实际入参仍收进 dict 走
    REGISTRY.call, 骨架 schema 随后被手写 params_schema 覆盖。
    """
    import functools

    def _run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        envelope = REGISTRY.call(spec.name, kwargs)
        if not envelope.get("ok"):
            from fastmcp.exceptions import ToolError
            raise ToolError(envelope.get("error", "工具执行失败"))
        return envelope
    return functools.wraps(spec.handler)(_run)


def build_fastmcp(read_only: bool = True, auth=None):
    """REGISTRY -> FastMCP 实例; 返回 (mcp, 注册工具数)。auth 为 fastmcp 鉴权器或 None"""
    from fastmcp import FastMCP
    from fastmcp.tools import Tool

    mcp = FastMCP(
        "alpha-finance-radar-mcp",
        instructions=(
            "A股投研工具集。股票类工具直接传股票名/代码(内部自解析); "
            "工具返回 ok=false 时把 error 告知用户, 不臆造数据; "
            "结果由调用方综合为中文结论, 不做投资建议。"),
        auth=auth,
    )
    n = 0
    for spec in REGISTRY.specs():
        if read_only and not spec.read_only:
            continue
        base = Tool.from_function(
            fn=_make_handler(spec),
            name=spec.name,
            description=spec.description,
            run_in_thread=True,
        )
        mcp.add_tool(base.model_copy(update={"parameters": spec.params_schema}))
        n += 1
    return mcp, n
