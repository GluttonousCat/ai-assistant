"""Sandboxed tool execution independent of routing and response composition."""
import asyncio
from typing import Any, Dict
from core.config import get_config

_cfg = get_config()
DEFAULT_TOOL_TIMEOUT = int(_cfg.get("agent.sandbox.default_tool_timeout", 120))
TOOL_TIMEOUTS = {"extract_document": 600, "read_report": 180, "analyze_chain": 300,
                 **{k: int(v) for k, v in (_cfg.get("agent.sandbox.tool_timeouts") or {}).items()}}

async def run_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Sandboxed execution."""
    from harness.mcp import get_registry
    registry = get_registry()
    timeout = TOOL_TIMEOUTS.get(name, DEFAULT_TOOL_TIMEOUT)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(registry.call, name, args), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "tool": name,
                "error": f"工具执行超时 (> {timeout}s)。"
                         f"若是全市场扫描类工具, 建议缩小范围或稍后再试"}


async def execute_batch(calls, runner=run_tool):
    """Preserve input order while executing independent calls concurrently."""
    return list(await asyncio.gather(*(runner(name, args) for name, args in calls)))
