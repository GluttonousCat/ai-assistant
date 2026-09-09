# -*- encoding: utf-8 -*-
"""sub-agent 分域路由单元测试 (不调 LLM/不连 DB)"""
from __future__ import annotations

from agent.subagent import (DOMAIN_DEFS, ESCALATE_TOOL, domain_manifest,
                            escalate_tool_manifest, route_domain)


# ---------- 路由规则 ----------

def test_route_single_domain():
    assert route_domain("中际旭创的营收和净利润") == "financial"
    assert route_domain("茅台最新的研报观点") == "report"
    assert route_domain("AI算力产业链有哪些环节") == "chain"
    assert route_domain("中际旭创的预期差") == "chain"
    assert route_domain("贵州茅台现在什么形态") == "quant"
    assert route_domain("中际旭创有没有财务风险") == "risk"
    assert route_domain("宁德时代属于哪个行业") == "entity"


def test_route_compound_and_miss_goes_full():
    """复合信号(≥2域)与未命中 -> None (全工具循环, 兜底永在)"""
    # 预测(报告域) + 形态(量化域) 复合
    assert route_domain("中际旭创的盈利预测和现在的形态怎么样") is None
    # 追问口语 (无域词)
    assert route_domain("环比竟然下降了，数据对吗？") is None
    # 定义/解释类一律全循环 (模型直接答, 不进域)
    assert route_domain("什么是市盈率") is None
    assert route_domain("毛利率是什么意思") is None
    assert route_domain("") is None
    assert route_domain("   ") is None


def test_route_never_locks_capability():
    """路由是缩小菜单而非能力闸门: 模糊问法不得被误锁进错误域"""
    for q in ("中际旭创怎么样", "帮我看下这家公司", "怎么样分析这个票"):
        assert route_domain(q) is None, q
    # 库外标的: 路由进 financial 可接受 (工具报"未识别到股票"的行为与全循环一致)
    assert route_domain("特斯拉的营收是多少") in (None, "financial")


# ---------- 域定义完整性 ----------

def test_domain_defs_well_formed():
    from mcp import get_registry
    registry = get_registry()
    registered = {s.name for s in registry.specs()}
    for key, d in DOMAIN_DEFS.items():
        assert d["label"] and d["prompt"] and d["keywords"]
        assert 2 <= len(d["tools"]) <= 4, f"{key} 域工具数应 2~4"
        for t in d["tools"]:
            assert t in registered, f"{key} 引用未注册工具 {t}"
            assert registry.get(t).read_only, f"{key} 引用非只读工具 {t}"
        assert "escalate_to_full_agent" in d["prompt"]


def test_domains_cover_most_tools():
    """6 域工具并集应覆盖 17 个只读工具的绝大多数 (未覆盖的应是合理的)"""
    covered = {t for d in DOMAIN_DEFS.values() for t in d["tools"]}
    uncovered = ({s.name for s in __import__("mcp").get_registry().specs()
                  if s.read_only} - covered)
    # forge_chain 是写类不在 17 内; 目前唯一未分组的是 get_schema? -> entity 已含
    assert uncovered == set() or uncovered <= {"get_schema"}, uncovered


# ---------- 域 manifest 与 escalate 伪工具 ----------

def test_domain_manifest_includes_escalate():
    tools = domain_manifest("report")
    names = [t["function"]["name"] for t in tools]
    assert names == DOMAIN_DEFS["report"]["tools"] + [ESCALATE_TOOL]
    # escalate 不进全局注册表 (它只是循环内的伪工具)
    from mcp import get_registry
    assert get_registry().get(ESCALATE_TOOL) is None


def test_domain_manifest_without_escalate():
    names = [t["function"]["name"] for t in domain_manifest("quant", False)]
    assert names == DOMAIN_DEFS["quant"]["tools"]


def test_escalate_manifest_shape():
    m = escalate_tool_manifest()
    assert m["type"] == "function"
    assert m["function"]["name"] == ESCALATE_TOOL
    assert "parameters" in m["function"]
