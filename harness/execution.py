"""Sandboxed tool execution independent of routing and response composition."""
import asyncio
import json
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


def model_tool_result(env, max_chars):
    """Give the model a bounded report excerpt with intact metadata and JSON."""
    def encode(value):
        return json.dumps(value, ensure_ascii=False, default=str)

    data = env.get("data")
    result = encode(env)
    if env.get("tool") == "read_report" and isinstance(data, dict) and "content_excerpt" in data:
        verified = data.get("body_status") in ("available", "partial")
        if verified and len(result) <= max_chars:
            return result
        fields = ("report_id", "title", "org", "publish_date", "material_type", "body_status",
                  "body_note", "pages_with_text", "page_count", "content_chars", "truncated")
        brief = {key: data[key] for key in fields if key in data}
        if verified and data.get("rating_source"):
            brief.update(rating=data.get("rating"), rating_source=data["rating_source"])
        evidence = data.get("lead_evidence")
        if verified and evidence and len(encode(evidence)) <= max_chars // 4:
            brief["lead_evidence"] = evidence
        brief["content_excerpt"] = ""
        message = {"ok": env["ok"], "tool": env["tool"], "data": brief}
        available = max(0, max_chars - len(encode(message)))
        excerpt = data["content_excerpt"]
        brief["content_excerpt"] = excerpt[:available]
        overflow = len(encode(message)) - max_chars
        if overflow > 0:
            brief["content_excerpt"] = brief["content_excerpt"][:max(0, len(brief["content_excerpt"]) - overflow)]
        if len(brief["content_excerpt"]) < len(excerpt):
            brief["truncated"] = True
        result = encode(message)
    return result if len(result) <= max_chars else result[:max_chars] + "…（工具结果节选）"
