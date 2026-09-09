# -*- encoding: utf-8 -*-
"""
MCP 工具注册表

- mcp_tool(...) 装饰器: 工具实现处直接声明式注册
- call(name, **kwargs): 统一入口, 计时 + 异常兜底, 返回统一信封
    {"ok": true, "tool": ..., "elapsed_ms": ..., "data": <handler 返回>}
    {"ok": false, "tool": ..., "error": "中文消息"}
- mcp_manifest() / openai_manifest(): 两种协议形态导出
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from mcp.spec import ToolError, ToolSpec


class MCPToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    # ---------- 注册 ----------

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重名: {spec.name}")
        self._tools[spec.name] = spec

    def tool(self, name: str, domain: str, description: str,
             params_schema: Dict[str, Any], examples: Optional[List[str]] = None,
             read_only: bool = True, notes: str = "") -> Callable:
        """装饰器形式注册"""
        def deco(fn: Callable) -> Callable:
            self.register(ToolSpec(
                name=name, domain=domain, description=description,
                params_schema=params_schema, handler=fn,
                examples=examples or [], read_only=read_only, notes=notes))
            return fn
        return deco

    # ---------- 查询 ----------

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
        """MCP 协议 tools/list 格式"""
        return [s.to_mcp() for s in self._tools.values()]

    def openai_manifest(self, include_write: bool = True) -> List[Dict[str, Any]]:
        """OpenAI/deepseek function-calling 格式 (默认全量; 写类工具可过滤)"""
        return [s.to_openai() for s in self._tools.values()
                if include_write or s.read_only]

    # ---------- 执行 ----------

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """统一调用入口。所有异常都转成中文错误信封, 不向上抛。"""
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "tool": name,
                    "error": f"未知工具: {name}。可用: {', '.join(self._tools)}"}
        args = {k: v for k, v in (arguments or {}).items() if v is not None}
        # 剔除 schema 未声明的参数 (防 LLM 幻觉参数导致 TypeError)
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
        except Exception as e:  # noqa: BLE001 兜底, 错误消息必须回传给 LLM
            return {"ok": False, "tool": name,
                    "error": f"工具内部错误 ({type(e).__name__}): {e}"}


# 全局单例: mcp/tools/ 各域模块 import 时向它注册
REGISTRY = MCPToolRegistry()
