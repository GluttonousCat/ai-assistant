# -*- encoding: utf-8 -*-
"""
mcp/ — MCP 工具层: 平台 18 个投研能力的标准化工具化

- spec.py     ToolSpec 规范 (name/中文description/JSON Schema/handler)
- registry.py 注册表 (装饰器注册 / 统一 call / 双协议导出)
- tools/      6 域 18 工具实现 (见 tools/__init__.py 注释)
- server.py   stdio MCP 服务 (JSON-RPC, 零依赖)
- bridge.py   OpenAI function-calling 桥 (内部 Agent Loop / deepseek 用)

用法:
    from mcp.tools import REGISTRY
    REGISTRY.call("query_financials", {"mode": "single",
                                       "stocks": ["贵州茅台"], "metrics": ["营收"]})
"""
from mcp.registry import REGISTRY
from mcp.spec import ToolError, ToolSpec


def get_registry():
    """拿到已注册全部工具的注册表 (幂等)。

    注意: 工具注册发生在 import mcp.tools 时。
    单独 from mcp.registry import REGISTRY 拿到的是空表 (仅定义处使用)。
    """
    import mcp.tools  # noqa: F401 导入即注册
    return REGISTRY


__all__ = ["REGISTRY", "get_registry", "ToolSpec", "ToolError"]
