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


def test_33_tools_registered():
    specs = REGISTRY.specs()
    assert len(specs) == 33      # 30 read-only + 3 write (forge_chain/fetch_annual_report/write_article)
    names = [s.name for s in specs]
    assert len(set(names)) == 33  # no duplicate names


def test_domains_cover_five_groups():
    domains = {s.domain for s in REGISTRY.specs()}
    assert domains == {"entity", "financial", "report", "chain", "risk"}


def test_schema_tool_keeps_qualified_relations_and_finds_column_keywords(monkeypatch):
    from tools.finance import schema_info

    class Catalog:
        def __init__(self, *, read_only):
            assert read_only

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch_all(self, query):
            return [
                {"table_schema": "stock", "table_name": "index_daily", "column_name": "close"},
                {"table_schema": "stock", "table_name": "intraday_bar", "column_name": "interval_minutes"},
                {"table_schema": "fin", "table_name": "index_daily", "column_name": "wrong_column"},
            ]

    monkeypatch.setattr(schema_info, "PgClient", Catalog)
    monkeypatch.setattr(schema_info, "_schema_info", None)
    answer = REGISTRY.call("get_schema", {"keyword": "interval_minutes"})
    assert answer["ok"] and "表 stock.intraday_bar:" in answer["data"]["schema_text"]
    assert "index_daily" not in answer["data"]["schema_text"]
    text = REGISTRY.call("get_schema", {"keyword": "stock.index_daily"})["data"]["schema_text"]
    assert "字段: close" in text and "wrong_column" not in text


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
    assert len(REGISTRY.openai_manifest(include_write=False)) == 30
    for entry in mcp_manifest:
        assert set(entry) == {"name", "description", "inputSchema"}
    for entry in openai_manifest:
        assert entry["type"] == "function"
        assert set(entry["function"]) == {"name", "description", "parameters"}
