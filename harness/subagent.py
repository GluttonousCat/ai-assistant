# -*- encoding: utf-8 -*-
"""
Sub-agent domain routing.

Motivation: presenting all 18 tools at once to a weaker model (deepseek flash tier)
makes the choice space large and mis-selection likely. The mainstream fix is not
splitting MCP servers (hosts flatten tool lists, so server splits are invisible to the
model) but **shrinking the menu**: route to a domain first, the sub-agent sees only its
2-4 domain tools.

Design (zero regression risk):
- Routing is pure L1 rules (milliseconds, no LLM call): single-domain high confidence ->
  domain sub-agent; miss / compound signal (>=2 domains) / knowledge chit-chat ->
  full-tool loop (i.e. existing behavior, the fallback always remains)
- Each sub-agent gets a domain-expert prompt (appended as system; global caliber knowledge kept)
- Escalation chain: the domain manifest includes an escalate_to_full_agent pseudo-tool;
  when the sub-agent finds it needs out-of-domain capability it calls it -> the same
  messages continue with the full tool manifest, context preserved

Domain-definition source of truth (landing the DSH "compose into config" idea):
config.yaml `agent.domains`; changing tool lists / keywords / prompts touches config
only, not code (effective after restart); if the config section is missing or wholly
invalid, the built-in defaults below apply (fresh clone runs); a structurally
incomplete single domain is just skipped with a warning — prefer missing over
mis-routing: a missed route is only a lost optimization, a wrong route is an incident.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

ESCALATE_TOOL = "escalate_to_full_agent"


def escalate_tool_manifest() -> Dict[str, Any]:
    """OpenAI manifest of the escalation pseudo-tool (injected into domain sub-agents only; never enters REGISTRY/MCP)."""
    return {
        "type": "function",
        "function": {
            "name": ESCALATE_TOOL,
            "description": (
                "把当前问题升级给全工具 Agent 处理。仅当本域工具确实无法回答"
                "(需要查其他领域的数据, 如财务域子Agent被问到研报观点)时调用, "
                "并在参数里说明还缺什么。能用本域工具解决的禁止调用。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string",
                               "description": "需要哪些域外能力/工具, 一句话"}
                },
                "required": ["reason"],
            },
        },
    }


# ============================================================
# Built-in default domain definitions (fallback when config.yaml agent.domains is missing)
# Structure: label / tool list / domain-expert prompt / L1 routing core keywords
# ============================================================

_DEFAULT_DOMAINS: Dict[str, Dict[str, Any]] = {
    "financial": {
        "label": "财务数据",
        "tools": ["query_financials", "query_main_business", "valuation_percentile"],
        "keywords": ["营收", "营业收入", "净利润", "毛利率", "净利率", "roe",
                     "净资产收益率", "市盈率", "市净率", "股价", "收盘价", "市值",
                     "换手率", "资产负债率", "流动比率", "速动比率", "每股收益",
                     "营业利润", "经营现金流", "总资产", "商誉", "eps", "成交额",
                     "靠什么赚钱", "收入结构", "主营构成", "估值贵不贵", "估值分位",
                     "估值水位", "估值"],
        "prompt": (
            "你是财务数据子Agent, 只配备 3 个数据工具。"
            "时间表述 (最近三年/2024年报) 与指标别名交给工具处理; "
            "查当期数值/时序/对比/行业排名用 query_financials, "
            "收入结构用 query_main_business, 历史估值水位用 valuation_percentile。"
            "需要研报观点/盈利预测/产业链/量化/排雷等域外能力时, "
            "调用 escalate_to_full_agent 说明。"),
    },
    "report": {
        "label": "研报观点",
        "tools": ["search_reports", "read_report", "get_forecasts", "extract_document"],
        "keywords": ["研报", "解读", "评级", "目标价", "盈利预测", "观点",
                     "黄金", "原油", "大宗商品", "贵金属", "分析师", "券商",
                     "上调", "下调", "分歧"],
        "prompt": (
            "你是研报观点子Agent。先 search_reports 检索清单 (关键词或标的), "
            "需要某篇细节/单篇问答用 read_report; "
            "未来期间盈利预测与分歧度用 get_forecasts (历史兑现校验是域外工具, 需要就升级)。"
            "综合多篇观点时标注机构与日期; 平台无覆盖时如实说明, 不臆造观点。"
            "用户给的本地文件才用 extract_document。"
            "需要财务数值/产业链/量化等域外能力时调用 escalate_to_full_agent。"),
    },
    "chain": {
        "label": "产业链与预期差",
        "tools": ["list_chains", "analyze_chain", "analyze_alpha"],
        "keywords": ["产业链", "链条", "环节", "上游", "下游", "受益环节",
                     "板块挖掘", "受益标的", "预期差", "分歧度", "拐点"],
        "prompt": (
            "你是产业链与预期差子Agent。链名拿不准先 list_chains; "
            "环节→标的映射与环节指数用 analyze_chain — **整链问题只调一次**"
            "(不带 node 参数即返回全部环节, 严禁按环节逐个调用); "
            "只有聚焦单环节细节时才带 node 参数补查。"
            "个股四象限综合 (预测分歧x财务动量x估值分位x研报观点) 用 analyze_alpha。"
            "只要单一维度 (仅估值分位/仅盈利预测) 属域外工具, 需要就升级。"
            "需要行情/研报细节/排雷等域外能力时调用 escalate_to_full_agent。"),
    },
    "risk": {
        "label": "风控校验",
        "tools": ["detect_financial_risk", "verify_forecasts"],
        "keywords": ["财务风险", "异常检测", "排雷", "财务造假", "风险排查",
                     "财务健康", "预测兑现", "预测准不准", "兑现率", "研报可信",
                     "兑现"],
        "prompt": (
            "你是风控校验子Agent。财务排雷用 detect_financial_risk "
            "(规则信号仅供筛查, 非审计结论, 回答时带此免责说明); "
            "券商历史预测与实际值对照用 verify_forecasts "
            "(只校验人民币口径+已出年报期间)。"
            "需要财务明细/研报/估值等域外能力时调用 escalate_to_full_agent。"),
    },
    "entity": {
        "label": "实体与日历",
        "tools": ["resolve_stock", "get_schema", "trading_calendar"],
        "keywords": ["是什么公司", "哪个公司", "属于哪个行业", "什么行业",
                     "行业归属", "股票代码", "交易日", "交易日历", "上一交易日",
                     "有什么数据", "数据字典"],
        "prompt": (
            "你是实体与数据知识子Agent, 处理公司身份/行业归属/交易日历/"
            "平台数据范围类问题。直接用本域工具作答, 简洁准确。"
            "涉及行情/财务/研报等分析类问题时调用 escalate_to_full_agent 转交。"),
    },
}


def _load_domain_defs() -> Dict[str, Dict[str, Any]]:
    """Read config.yaml agent.domains; missing/wholly invalid falls back to built-in defaults; a single invalid domain is skipped."""
    from core.config import get_config
    from core.logger import get_logger
    logger = get_logger(__name__)

    raw = get_config().get("agent.domains")
    if not isinstance(raw, dict) or not raw:
        return {k: dict(v) for k, v in _DEFAULT_DOMAINS.items()}

    defs: Dict[str, Dict[str, Any]] = {}
    for key, d in raw.items():
        ok = (isinstance(d, dict)
              and isinstance(d.get("label"), str) and d["label"]
              and isinstance(d.get("prompt"), str) and d["prompt"]
              and isinstance(d.get("tools"), list) and 1 <= len(d["tools"]) <= 8
              and isinstance(d.get("keywords"), list) and d["keywords"])
        if not ok:
            logger.warning(f"agent.domains.{key} 结构不完整 "
                           f"(需 label/prompt/tools/keywords), 该域已跳过")
            continue
        defs[key] = d
    if not defs:
        logger.warning("agent.domains 无任何合法域, 回落内置域定义")
        return {k: dict(v) for k, v in _DEFAULT_DOMAINS.items()}
    return defs


DOMAIN_DEFS: Dict[str, Dict[str, Any]] = _load_domain_defs()


def domain_manifest(domain: str, with_escalate: bool = True) -> List[Dict[str, Any]]:
    """Domain-tools OpenAI manifest (includes the escalate pseudo-tool; no REGISTRY state check, pure filtering)."""
    from harness.mcp import get_registry
    registry = get_registry()
    names = DOMAIN_DEFS[domain]["tools"]
    tools = [s.to_openai() for s in registry.specs() if s.name in names]
    if with_escalate:
        tools.append(escalate_tool_manifest())
    return tools


def route_domain(text: str) -> Optional[str]:
    """Pure L1 rule routing: returns a domain key or None (None = full-tool loop).

    Rule: the set of domains whose core keywords hit; exactly 1 -> that domain;
    >=2 -> None (compound, left to the full-tool loop to combine freely); 0 -> None.
    Prefer missing over mis-routing: routing is a menu-shrinking optimization, not a
    capability gate — a missed route is only a lost optimization, a wrong route is an incident.
    Definition/explanation phrasing (什么是市盈率) always goes to the full loop —
    the model answers directly and should enter no domain.
    """
    text_l = (text or "").lower()
    if not text_l.strip():
        return None
    if any(w in text_l for w in ("什么是", "是什么", "什么意思", "解释一下",
                                 "定义", "为什么")):
        return None
    hits = set()
    for key, defn in DOMAIN_DEFS.items():
        for kw in defn["keywords"]:
            if kw in text_l:
                hits.add(key)
                break
    if len(hits) == 1:
        return next(iter(hits))
    return None
