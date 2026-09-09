# -*- encoding: utf-8 -*-
"""mcp/ 单元测试: 注册表完整性与信封契约 (不依赖 DB, 秒级)

运行: .venv\\Scripts\\python.exe -m pytest mcp/tests -q
数据相关的端到端验证用 scripts/smoke_mcp_tools.py (需要 PG)。
"""
from __future__ import annotations

import pytest

from mcp import get_registry
from mcp.spec import ToolError

REGISTRY = get_registry()


def test_18_tools_registered():
    specs = REGISTRY.specs()
    assert len(specs) == 19      # 18 只读 + fetch_annual_report (写类, 巨潮爬虫)
    names = [s.name for s in specs]
    assert len(set(names)) == 19  # 无重名


def test_domains_cover_six_groups():
    domains = {s.domain for s in REGISTRY.specs()}
    assert domains == {"entity", "financial", "report", "chain", "quant", "risk"}


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda s: s.name)
def test_spec_well_formed(spec):
    """每个工具: 中文 description / 合法 schema / handler 可调"""
    assert spec.description and len(spec.description) >= 20
    assert any("\u4e00" <= ch <= "\u9fff" for ch in spec.description)  # 含中文
    schema = spec.params_schema
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    assert all(isinstance(v, dict) for v in props.values())
    for req in schema.get("required", []):
        assert req in props
    assert callable(spec.handler)


def test_call_unknown_tool_envelope():
    env = REGISTRY.call("no_such_tool", {})
    assert env["ok"] is False
    assert "no_such_tool" in env["error"]
    assert "resolve_stock" in env["error"]  # 错误里带可用工具提示


def test_call_strips_undeclared_params():
    # 未声明参数被剔除, warnings 提示 (handler 必然缺参 -> ToolError 而非 TypeError)
    @REGISTRY.tool(name="_t_strip", domain="entity", description="测试用临时工具",
                   params_schema={"type": "object",
                                  "properties": {"x": {"type": "string"}}})
    def _t(x: str = ""):
        return {"x": x}

    try:
        env = REGISTRY.call("_t_strip", {"x": "a", "hallucinated": 1})
        assert env["ok"] is True
        assert env["data"] == {"x": "a"}
        assert env["warnings"] and "hallucinated" in env["warnings"][0]
    finally:
        REGISTRY._tools.pop("_t_strip", None)


def test_tool_error_becomes_chinese_envelope():
    @REGISTRY.tool(name="_t_err", domain="entity", description="测试用临时工具",
                   params_schema={"type": "object", "properties": {}})
    def _t():
        raise ToolError("业务错误消息")

    try:
        env = REGISTRY.call("_t_err", {})
        assert env["ok"] is False
        assert env["error"] == "业务错误消息"
    finally:
        REGISTRY._tools.pop("_t_err", None)


def test_manifests_consistent():
    mcp_manifest = REGISTRY.mcp_manifest()
    openai_manifest = REGISTRY.openai_manifest()
    assert len(mcp_manifest) == len(REGISTRY.specs())
    assert len(openai_manifest) == len(REGISTRY.specs())
    # 只读过滤: 排除 forge_chain 后 17 个
    assert len(REGISTRY.openai_manifest(include_write=False)) == 17
    for entry in mcp_manifest:
        assert set(entry) == {"name", "description", "inputSchema"}
    for entry in openai_manifest:
        assert entry["type"] == "function"
        assert set(entry["function"]) == {"name", "description", "parameters"}
