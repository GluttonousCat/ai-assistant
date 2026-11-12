# -*- encoding: utf-8 -*-
"""REGISTRY -> FastMCP adapter layer (enterprise exit: shape conversion for stdio + streamable HTTP).

- Zero change to the tool layer: ToolSpec (harness/mcp/spec.py) remains the single source
  of truth; the dual manifests (to_mcp/to_openai) and this adapter coexist as three shapes;
- Chinese parameter schemas preserved: Tool.from_function derives the skeleton from the
  signature -> model_copy overrides it with the hand-written params_schema (with Chinese
  parameter descriptions — key to LLM argument-selection quality);
- Execution still goes through the unified REGISTRY.call envelope (timing / hallucinated-arg
  pruning / Chinese error fallback); ok=False raises fastmcp ToolError -> protocol isError
  (equivalent to legacy server.py behavior);
- read_only sandbox: read-only mode registers only read_only tools (same-source convention
  as ALPHA_MCP_READ_ONLY);
- Synchronous handler + run_in_thread=True: fastmcp thread-pool execution, no async
  refactor needed (storage.pg PgPool is already thread-safe).
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Tuple

from harness.mcp.spec import ToolSpec
from harness.mcp.tools import REGISTRY


def read_only_mode() -> bool:
    """Read-only sandbox switch (same source as legacy server.py: ALPHA_MCP_READ_ONLY, default on)."""
    return os.environ.get("ALPHA_MCP_READ_ONLY", "1") not in ("0", "false", "False")


def _make_handler(spec: ToolSpec) -> Callable[..., Dict[str, Any]]:
    """Wraps: unified envelope + ok=False converted to ToolError (isError semantics).

    functools.wraps(handler) lets from_function resolve the tool's explicit parameter
    signature through __wrapped__ (fastmcp does not support **kwargs signatures); the
    actual arguments are still collected into a dict and go through REGISTRY.call, and the
    skeleton schema is subsequently overridden by the hand-written params_schema.
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
    """REGISTRY -> FastMCP instance; returns (mcp, number of registered tools). auth is a fastmcp authenticator or None."""
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
