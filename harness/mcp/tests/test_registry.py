# -*- encoding: utf-8 -*-
"""mcp/ unit tests: registry integrity and envelope contracts (no DB dependency, seconds).

Run: .venv\\Scripts\\python.exe -m pytest mcp/tests -q
For data-dependent end-to-end verification use jobs/smoke_mcp_tools.py (requires PG).
"""
from __future__ import annotations

import pytest

from harness.mcp import get_registry
from harness.mcp.spec import ToolError

REGISTRY = get_registry()


def test_32_tools_registered():
    specs = REGISTRY.specs()
    assert len(specs) == 32      # 29 read-only + 3 write (forge_chain/fetch_annual_report/write_article)
    names = [s.name for s in specs]
    assert len(set(names)) == 32  # no duplicate names


def test_domains_cover_five_groups():
    domains = {s.domain for s in REGISTRY.specs()}
    assert domains == {"entity", "financial", "report", "chain", "risk"}


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda s: s.name)
def test_spec_well_formed(spec):
    """Every tool: Chinese description / valid schema / callable handler."""
    assert spec.description and len(spec.description) >= 20
    assert any("\u4e00" <= ch <= "\u9fff" for ch in spec.description)  # contains Chinese
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
    assert "resolve_stock" in env["error"]  # error includes a hint of available tools


def test_call_strips_undeclared_params():
    # Undeclared params are stripped with a warnings hint (handler necessarily misses the arg -> ToolError, not TypeError)
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
    # Read-only filter: 29 read-only (3 write tools excluded)
    assert len(REGISTRY.openai_manifest(include_write=False)) == 29
    for entry in mcp_manifest:
        assert set(entry) == {"name", "description", "inputSchema"}
    for entry in openai_manifest:
        assert entry["type"] == "function"
        assert set(entry["function"]) == {"name", "description", "parameters"}
