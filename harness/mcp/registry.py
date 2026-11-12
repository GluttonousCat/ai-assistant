# -*- encoding: utf-8 -*-
"""
MCP tool registry.

- mcp_tool(...) decorator: declarative registration right at the tool implementation
- call(name, **kwargs): unified entry, timing + exception fallback, returns a unified envelope
    {"ok": true, "tool": ..., "elapsed_ms": ..., "data": <handler return>}
    {"ok": false, "tool": ..., "error": "Chinese message"}
- mcp_manifest() / openai_manifest(): export in the two protocol shapes
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from harness.mcp.spec import ToolError, ToolSpec


class MCPToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    # ---------- Registration ----------

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重名: {spec.name}")
        self._tools[spec.name] = spec

    def tool(self, name: str, domain: str, description: str,
             params_schema: Dict[str, Any], examples: Optional[List[str]] = None,
             read_only: bool = True, notes: str = "") -> Callable:
        def deco(fn: Callable) -> Callable:
            self.register(
                ToolSpec(
                    name=name, domain=domain, description=description,
                    params_schema=params_schema, handler=fn,
                    examples=examples or [], read_only=read_only, notes=notes
                )
            )
            return fn
        return deco

    # ---------- Queries ----------

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def specs(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def by_domain(self) -> Dict[str, List[ToolSpec]]:
        out: Dict[str, List[ToolSpec]] = {}
        for s in self._tools.values():
            out.setdefault(s.domain, []).append(s)
        return out

    def mcp_manifest(self) -> List[Dict[str, Any]]:
        """MCP protocol tools/list format."""
        return [s.to_mcp() for s in self._tools.values()]

    def openai_manifest(self, include_write: bool = True) -> List[Dict[str, Any]]:
        """OpenAI/deepseek function-calling format (all tools by default; write tools can be filtered out)."""
        return [s.to_openai() for s in self._tools.values()
                if include_write or s.read_only]

    # ---------- Execution ----------

    def call(
            self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Unified invocation entry. All exceptions become a Chinese error envelope; nothing propagates upward."""
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "tool": name,
                    "error": f"未知工具: {name}。可用: {', '.join(self._tools)}"}
        args = {k: v for k, v in (arguments or {}).items() if v is not None}
        # Drop parameters not declared in the schema (guards against LLM-hallucinated args causing TypeError)
        known = spec.params_schema.get("properties", {})
        unknown = [k for k in args if k not in known]
        if unknown:
            args = {k: v for k, v in args.items() if k in known}
        t0 = time.time()
        try:
            data = spec.handler(**args)
            return {"ok": True, "tool": name,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "warnings": ([f"忽略未声明参数: {', '.join(unknown)}"]
                                 if unknown else []),
                    "data": data}
        except ToolError as e:
            return {"ok": False, "tool": name,
                    "error": str(e)}
        except TypeError as e:
            return {"ok": False, "tool": name,
                    "error": f"参数不匹配: {e}"}
        except Exception as e:  # noqa: BLE001 catch-all; the error message must be returned to the LLM
            return {"ok": False, "tool": name,
                    "error": f"工具内部错误 ({type(e).__name__}): {e}"}


REGISTRY = MCPToolRegistry()
