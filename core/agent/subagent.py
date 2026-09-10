# -*- encoding: utf-8 -*-
"""
子 Agent 分域路由 (Sub-Agent Domain Routing)

动机: 18 个工具一次摊给 LLM, 弱模型 (deepseek flash 档) 选择面大易误选。
主流做法不是拆 MCP server (宿主会把工具清单拍平, 拆 server 对模型不可见),
而是**缩小菜单**: 先路由到领域, 子 Agent 只看本域 2~4 个工具。

设计 (零回归风险):
- 路由用 L1 纯规则 (毫秒级, 不调 LLM): 单域高置信 -> 域子 Agent;
  未命中 / 复合信号(≥2 域) / 知识闲聊 -> 全工具循环 (即现有行为, 兜底永在)
- 每个子 Agent 附带域专家提示词 (追加 system, 全局口径知识保留)
- 升级链路: 域清单里放一个 escalate_to_full_agent 伪工具,
  子 Agent 发现需要域外能力时调用 -> 同一 messages 换全工具清单续跑, 上下文不丢
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp.spec import ToolSpec

ESCALATE_TOOL = "escalate_to_full_agent"


def escalate_tool_manifest() -> Dict[str, Any]:
    """升级伪工具的 OpenAI manifest (仅注入域子 Agent, 不进 REGISTRY/MCP)"""
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
# 域定义: 标签 / 工具清单 / 域专家提示词 / L1 路由核心词
# ============================================================

DOMAIN_DEFS: Dict[str, Dict[str, Any]] = {
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
    "quant": {
        "label": "量化形态",
        "tools": ["compute_indicators", "run_quant_scan"],
        "keywords": ["形态", "区间状态", "震荡区间", "趋势状态", "adx", "poc",
                     "wyckoff", "弹簧", "技术指标", "全市场扫描", "蓄势盾"],
        "prompt": (
            "你是量化形态子Agent。单标的当前形态/区间状态用 run_quant_scan 的 regime"
            " (秒级); 要 ADX/POC/Wyckoff 指标数值用 compute_indicators; "
            "全市场扫描 (range/trend/setup) 分钟级, 用户明确要才用。"
            "需要财务/研报/产业链等域外能力时调用 escalate_to_full_agent。"),
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


def domain_manifest(domain: str, with_escalate: bool = True) -> List[Dict[str, Any]]:
    """域工具 OpenAI manifest (含 escalate 伪工具; 不查 REGISTRY 状态, 纯过滤)"""
    from mcp import get_registry
    registry = get_registry()
    names = DOMAIN_DEFS[domain]["tools"]
    tools = [s.to_openai() for s in registry.specs() if s.name in names]
    if with_escalate:
        tools.append(escalate_tool_manifest())
    return tools


def route_domain(text: str) -> Optional[str]:
    """L1 纯规则路由: 返回域 key 或 None (None=全工具循环)。

    规则: 核心词命中的域集合; 恰好 1 个 -> 该域; >=2 个 -> None (复合, 交给
    全工具循环自由组合); 0 个 -> None。宁漏勿误: 路由是缩小菜单的优化,
    不是能力闸门 — 漏路由只是少优化, 误路由才是事故。
    定义/解释类问法 (什么是市盈率) 一律全循环 — 模型直接答, 不该进任何域。
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
