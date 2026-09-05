"""
财务分析 Agent 主图
意图识别 -> Skill 路由 -> 执行 -> 响应
(Text-to-SQL 链路: schema linking -> SQL 生成 -> 校验 -> 执行 -> 解读)

设计:
- 意图识别: LLM Router (有 key 时) 或 关键词规则 (降级)
- Skill: 复用 skills.fin_query.FinQuerySkill (对比/异常/校验后续叠加)
- 输出: 结构化响应 (summary + data + sql)
"""
from __future__ import annotations

from typing import Dict, Literal, Optional

from langgraph.graph import END, StateGraph

from agent.state import AgentState
from agent.intent import LayeredIntentClassifier, get_intent_classifier
from skills.base import SkillContext
from skills.fin_query.skill import FinQuerySkill
from skills.report.skill import ReportSkill
from beta_alpha.skills.beta import BetaSkill
from beta_alpha.skills.alpha import AlphaSkill


def parse_financial_intent_node(state: AgentState) -> AgentState:
    """意图识别节点 (分层: 规则 L1 + LLM L2)"""
    clf = get_intent_classifier()
    result = clf.classify(state.user_input)
    state.log(
        f"财务意图识别: {result.intent} "
        f"(conf={result.confidence:.2f}, source={result.source})"
    )
    # 存入 AgentState.parsed_intent (LangGraph schema 认可的字段)
    state.parsed_intent = result.to_state_dict()
    return state


def route_by_fin_intent(
    state: AgentState,
) -> Literal["execute_query", "report", "chain", "alpha", "respond"]:
    """路由: query/compare 走 SQL 链路, report 走研报分析,
    chain 走产业链Beta, alpha 走个股预期差, 其他返回占位"""
    intent = (state.parsed_intent or {}).get("type", "unknown")
    if intent in ("query", "compare"):
        return "execute_query"
    if intent == "report":
        return "report"
    if intent == "chain":
        return "chain"
    if intent == "alpha":
        return "alpha"
    return "respond"


def execute_query_node(state: AgentState) -> AgentState:
    """执行 Text-to-SQL 查询 (携带会话上下文股票, 支持省略主语追问)"""
    skill = FinQuerySkill()
    ctx = SkillContext(user_input=state.user_input)
    if state.context_stocks:
        ctx.params = {"context_stocks": state.context_stocks}
    ctx = skill(ctx)

    if ctx.error:
        state.response = f"❌ {ctx.error}"
        return state

    state.fin_result = ctx.result
    state.response = f"{ctx.result.get('summary', '')}"
    return state


def report_node(state: AgentState) -> AgentState:
    """研报分析节点 (ReportSkill: 综合解读/问答/提取由输入形态决定)"""
    skill = ReportSkill()
    params = dict((state.parsed_intent or {}).get("slots") or {})
    ctx = SkillContext(user_input=state.user_input, params=params)
    ctx = skill(ctx)

    if ctx.error:
        state.response = f"❌ {ctx.error}"
        return state

    result = ctx.result or {}
    state.fin_result = result if isinstance(result, dict) else {"summary": result}
    state.response = result.get("summary", "") if isinstance(result, dict) else str(result)
    return state


def chain_node(state: AgentState) -> AgentState:
    """产业链Beta节点 (BetaSkill: 链解析->三源映射->环节指标->综述)"""
    skill = BetaSkill()
    ctx = SkillContext(user_input=state.user_input)
    ctx = skill(ctx)

    if ctx.error:
        state.response = f"❌ {ctx.error}"
        return state

    result = ctx.result or {}
    state.fin_result = result
    state.response = result.get("summary", "")
    return state


def alpha_node(state: AgentState) -> AgentState:
    """个股预期差节点 (AlphaSkill: 预测分歧x财务动量x估值分位x研报观点)"""
    skill = AlphaSkill()
    ctx = SkillContext(user_input=state.user_input)
    ctx = skill(ctx)

    if ctx.error:
        state.response = f"❌ {ctx.error}"
        return state

    result = ctx.result or {}
    state.fin_result = result
    state.response = result.get("summary", "")
    return state


def respond_node(state: AgentState) -> AgentState:
    """最终响应 (其他意图占位/兜底)"""
    intent = (state.parsed_intent or {}).get("type", "unknown")
    if not state.response:
        if intent == "compare":
            state.response = "多维度对比分析功能开发中 (M2)"
        elif intent == "detect":
            state.response = "财务异常检测功能开发中 (M3)"
        elif intent == "verify":
            state.response = "研报一致性校验功能开发中 (M4)"
        elif intent == "report":
            state.response = "报告生成功能开发中 (M5)"
        elif intent == "unknown":
            state.response = "无法识别意图。支持的自然语言查询示例：\n- 查询平安银行的营收\n- 看看贵州茅台的毛利率\n- 解读贵州茅台的研报"
    return state


# ============================================================
# 图构建
# ============================================================

def create_financial_agent():
    """创建财务分析 Agent 图"""
    workflow = StateGraph(AgentState)

    workflow.add_node("parse_intent", parse_financial_intent_node)
    workflow.add_node("execute_query", execute_query_node)
    workflow.add_node("report", report_node)
    workflow.add_node("chain", chain_node)
    workflow.add_node("alpha", alpha_node)
    workflow.add_node("respond", respond_node)

    workflow.set_entry_point("parse_intent")

    workflow.add_conditional_edges(
        "parse_intent",
        route_by_fin_intent,
        {"execute_query": "execute_query", "report": "report",
         "chain": "chain", "alpha": "alpha", "respond": "respond"},
    )

    workflow.add_edge("execute_query", "respond")
    workflow.add_edge("report", "respond")
    workflow.add_edge("chain", "respond")
    workflow.add_edge("alpha", "respond")
    workflow.add_edge("respond", END)

    return workflow.compile()


def invoke_financial_agent(user_input: str, session_id: Optional[str] = None) -> AgentState:
    """便捷调用
    session_id: 会话 ID. 提供后启用跨轮槽位继承 --
    当前句无股票实体时沿用上轮股票 (如 "营收跟净利是降速, 不对劲吧?")"""
    from agent.graph import invoke_agent
    from agent.context_store import get_session_stocks, update_session_from_result
    agent = create_financial_agent()
    state = AgentState()
    state.user_input = user_input
    state.session_id = session_id
    state.context_stocks = get_session_stocks(session_id)
    result = invoke_agent(agent, state)
    # 写回: 本轮明确提及股票才更新 (继承轮保留原上下文)
    if session_id:
        update_session_from_result(
            session_id, user_input, (result.fin_result or {}).get("data"))
    return result


if __name__ == "__main__":
    result = invoke_financial_agent("查询平安银行的营收")
    print("response:", result.response[:300])