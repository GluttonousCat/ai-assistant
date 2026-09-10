#!/usr/bin/env python3
"""
LangGraph工作流定义
"""
from typing import Literal
from langgraph.graph import StateGraph, END

from .state import AgentState
from .nodes import (
    parse_intent_node,
    check_cookie_node,
    create_task_node,
    execute_crawl_node,
    handle_cookie_input_node,
    generate_response_node
)


def route_by_intent(state: AgentState) -> Literal["check_cookie", "handle_cookie", "respond"]:
    """
    根据意图路由
    """
    if not state.parsed_intent:
        return "respond"

    intent = state.parsed_intent
    action = intent.get("action")

    # Cookie相关指令
    if action in ["set_cookie", "update_cookie"] or "cookie" in state.user_input.lower():
        return "handle_cookie"

    # 需要Cookie的爬取指令
    if action in ["latest", "historical", "incremental", "update", "files"]:
        return "check_cookie"

    return "respond"


def route_after_check(state: AgentState) -> Literal["create_task", "respond"]:
    """
    Cookie检查后路由
    """
    cookie = state.get_active_cookie()
    if cookie:
        return "create_task"
    return "respond"


def route_after_create(state: AgentState) -> Literal["execute", "respond"]:
    """
    任务创建后路由
    """
    if state.current_task:
        return "execute"
    return "respond"


def create_crawler_agent():
    """
    创建爬虫Agent工作流
    """
    # 创建工作流
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node("parse_intent", parse_intent_node)
    workflow.add_node("check_cookie", check_cookie_node)
    workflow.add_node("handle_cookie", handle_cookie_input_node)
    workflow.add_node("create_task", create_task_node)
    workflow.add_node("execute", execute_crawl_node)
    workflow.add_node("respond", generate_response_node)

    # 设置入口
    workflow.set_entry_point("parse_intent")

    # 添加条件边
    workflow.add_conditional_edges(
        "parse_intent",
        route_by_intent,
        {
            "check_cookie": "check_cookie",
            "handle_cookie": "handle_cookie",
            "respond": "respond"
        }
    )

    workflow.add_conditional_edges(
        "check_cookie",
        route_after_check,
        {
            "create_task": "create_task",
            "respond": "respond"
        }
    )

    workflow.add_conditional_edges(
        "create_task",
        route_after_create,
        {
            "execute": "execute",
            "respond": "respond"
        }
    )

    # 添加结束边
    workflow.add_edge("handle_cookie", "respond")
    workflow.add_edge("execute", "respond")
    workflow.add_edge("respond", END)

    # 编译
    return workflow.compile()


# 便捷函数
def invoke_agent(agent, state: AgentState) -> AgentState:
    """
    执行 Agent 并把 LangGraph 返回的 dict 重建为 AgentState
    (dataclass 作为 state schema 时, invoke 返回字段字典)
    """
    result = agent.invoke(state)
    if isinstance(result, dict):
        result = AgentState(**result)
    return result


def run_agent(user_input: str, state: AgentState = None) -> AgentState:
    """
    运行Agent
    """
    if state is None:
        state = AgentState()

    state.user_input = user_input

    agent = create_crawler_agent()
    return invoke_agent(agent, state)
