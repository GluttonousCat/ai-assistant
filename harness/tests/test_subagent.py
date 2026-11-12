# -*- encoding: utf-8 -*-
"""Sub-agent domain routing unit tests (no LLM calls / no DB)."""
from __future__ import annotations

from harness.subagent import (DOMAIN_DEFS, ESCALATE_TOOL, domain_manifest,
                            escalate_tool_manifest, route_domain)


# ---------- Routing rules ----------

def test_route_single_domain():
    assert route_domain("中际旭创的营收和净利润") == "financial"
    assert route_domain("茅台最新的研报观点") == "report"
    assert route_domain("AI算力产业链有哪些环节") == "chain"
    assert route_domain("中际旭创的预期差") == "chain"
    assert route_domain("贵州茅台现在什么形态") == "quant"
    assert route_domain("中际旭创有没有财务风险") == "risk"
    assert route_domain("宁德时代属于哪个行业") == "entity"


def test_route_compound_and_miss_goes_full():
    """Compound signals (>=2 domains) and misses -> None (full-tool loop; the fallback always remains)."""
    # forecast (report domain) + pattern (quant domain) compound
    assert route_domain("中际旭创的盈利预测和现在的形态怎么样") is None
    # colloquial follow-up (no domain word)
    assert route_domain("环比竟然下降了，数据对吗？") is None
    # definition/explanation phrasing always goes to the full loop (model answers directly, enters no domain)
    assert route_domain("什么是市盈率") is None
    assert route_domain("毛利率是什么意思") is None
    assert route_domain("") is None
    assert route_domain("   ") is None


def test_route_never_locks_capability():
    """Routing shrinks the menu but is not a capability gate: vague phrasing must not be locked into a wrong domain."""
    for q in ("中际旭创怎么样", "帮我看下这家公司", "怎么样分析这个票"):
        assert route_domain(q) is None, q
    # Out-of-database target: routing into financial is acceptable (the tool's
    # "stock not recognized" behavior matches the full loop)
    assert route_domain("特斯拉的营收是多少") in (None, "financial")


# ---------- Domain definition integrity ----------

def test_domain_defs_well_formed():
    from harness.mcp import get_registry
    registry = get_registry()
    registered = {s.name for s in registry.specs()}
    for key, d in DOMAIN_DEFS.items():
        assert d["label"] and d["prompt"] and d["keywords"]
        assert 2 <= len(d["tools"]) <= 8, \
            f"{key} domain tool count should be 2~8 (upper bound checked by subagent._load_domain_defs)"
        for t in d["tools"]:
            assert t in registered, f"{key} references unregistered tool {t}"
            assert registry.get(t).read_only, f"{key} references non-read-only tool {t}"
        assert "escalate_to_full_agent" in d["prompt"]


def test_domains_cover_most_tools():
    """The union of the 6 domains' tools should cover most read-only tools (uncovered ones should be justifiable)."""
    from harness.mcp import get_registry
    covered = {t for d in DOMAIN_DEFS.values() for t in d["tools"]}
    uncovered = ({s.name for s in get_registry().specs()
                  if s.read_only} - covered)
    # Allowed to stay ungrouped: cross-domain composite tools (e.g. build_company_profile's
    # eight-section profile) — the full loop's home turf
    assert uncovered <= {"get_schema", "build_company_profile", "extract_pdf_tables", "read_annual_profile"}, uncovered


# ---------- Domain manifest and the escalate pseudo-tool ----------

def test_domain_manifest_includes_escalate():
    tools = domain_manifest("report")
    names = [t["function"]["name"] for t in tools]
    # manifest order = registration order, independent of the config list order; compare as sets
    assert set(names) == set(DOMAIN_DEFS["report"]["tools"]) | {ESCALATE_TOOL}
    assert names[-1] == ESCALATE_TOOL  # escalate is always appended last
    # escalate never enters the global registry (it is only a pseudo-tool inside the loop)
    from harness.mcp import get_registry
    assert get_registry().get(ESCALATE_TOOL) is None


def test_domain_manifest_without_escalate():
    names = [t["function"]["name"] for t in domain_manifest("quant", False)]
    assert names == DOMAIN_DEFS["quant"]["tools"]


def test_escalate_manifest_shape():
    m = escalate_tool_manifest()
    assert m["type"] == "function"
    assert m["function"]["name"] == ESCALATE_TOOL
    assert "parameters" in m["function"]
