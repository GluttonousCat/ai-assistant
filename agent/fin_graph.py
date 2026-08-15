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
from skills.base import SkillContext
from skills.fin_query.skill import FinQuerySkill


# ============================================================
# 意图识别
# ============================================================

INTENT_RULES: Dict[str, list] = {
    "query": ["查询", "查一下", "多少", "营收", "净利润", "roe", "毛利率",
              "排名", "筛选", "大于", "小于", "同比", "对比",
              "股价", "收盘", "市盈", "市净", "市值", "涨跌幅", "换手"],
    "detect": ["风险", "异常", "预警", "排雷", "检测"],
    "verify": ["校验", "验证", "研报", "一致性"],
    "report": ["生成", "报告", "分析报告", "深度报告"],
}


def classify_intent(user_input: str) -> str:
    """关键词意图识别 (LLM 降级方案)"""
    text = user_input.lower()
    # 优先级: report > detect > verify > query
    for intent, kws in INTENT_RULES.items():
        if any(k in text for k in kws):
            return intent
    return "unknown"


def parse_financial_intent_node(state: AgentState) -> AgentState:
    """意图识别节点"""
    intent = classify_intent(state.user_input)
    state.log(f"财务意图识别: {intent}")
    # 存入 AgentState.parsed_intent (LangGraph schema 认可的字段)
    state.parsed_intent = {"type": intent, "text": state.user_input}
    return state


def route_by_fin_intent(state: AgentState) -> Literal["execute_query", "respond"]:
    """路由: 只有 query 走 SQL 链路, 其他当前返回占位"""
    intent = (state.parsed_intent or {}).get("type", "unknown")
    if intent == "query":
        return "execute_query"
    return "respond"


def execute_query_node(state: AgentState) -> AgentState:
    """执行 Text-to-SQL 查询"""
    skill = FinQuerySkill()
    ctx = SkillContext(user_input=state.user_input)
    ctx = skill(ctx)

    if ctx.error:
        state.response = f"❌ {ctx.error}"
        return state

    state.fin_result = ctx.result
    state.response = f"{ctx.result.get('summary', '')}"
    return state


def respond_node(state: AgentState) -> AgentState:
    """最终响应 (其他意图占位/兜底)"""
    intent = (state.parsed_intent or {}).get("type", "unknown")
    if not state.response:
        if intent == "detect":
            state.response = "财务异常检测功能开发中 (M3)"
        elif intent == "verify":
            state.response = "研报一致性校验功能开发中 (M4)"
        elif intent == "report":
            state.response = "报告生成功能开发中 (M5)"
        elif intent == "unknown":
            state.response = "无法识别意图。支持的自然语言查询示例：\n- 查询平安银行的营收\n- 看看贵州茅台的毛利率\n- 查询宁德时代的净利润"
    return state


# ============================================================
# 图构建
# ============================================================

def create_financial_agent():
    """创建财务分析 Agent 图"""
    workflow = StateGraph(AgentState)

    workflow.add_node("parse_intent", parse_financial_intent_node)
    workflow.add_node("execute_query", execute_query_node)
    workflow.add_node("respond", respond_node)

    workflow.set_entry_point("parse_intent")

    workflow.add_conditional_edges(
        "parse_intent",
        route_by_fin_intent,
        {"execute_query": "execute_query", "respond": "respond"},
    )

    workflow.add_edge("execute_query", "respond")
    workflow.add_edge("respond", END)

    return workflow.compile()


def invoke_financial_agent(user_input: str) -> AgentState:
    """便捷调用"""
    from agent.graph import invoke_agent
    agent = create_financial_agent()
    state = AgentState()
    state.user_input = user_input
    return invoke_agent(agent, state)


if __name__ == "__main__":
    result = invoke_financial_agent("查询平安银行的营收")
    print("response:", result.response[:300])