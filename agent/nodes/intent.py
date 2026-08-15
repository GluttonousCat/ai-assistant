"""
意图解析节点
"""
import re

from agent.state import AgentState


def parse_intent_node(state: AgentState) -> AgentState:
    """
    意图解析节点
    分析用户输入, 提取爬取意图
    TODO(阶段二): 升级为 LLM 意图识别, 并路由到四大 Skill
    """
    state.log(f"解析意图: {state.user_input}")

    intent = {
        "action": None,
        "group_id": None,
        "params": {},
    }

    text = state.user_input.lower()

    if any(kw in text for kw in ["最新", "new", "latest"]):
        intent["action"] = "latest"
    elif any(kw in text for kw in ["历史", "history", "historical"]):
        intent["action"] = "historical"
    elif any(kw in text for kw in ["增量", "incremental", "更新"]):
        intent["action"] = "incremental"
    elif any(kw in text for kw in ["文件", "file", "下载"]):
        intent["action"] = "files"
    elif any(kw in text for kw in ["状态", "统计", "status", "stats"]):
        intent["action"] = "stats"
    else:
        intent["action"] = "unknown"

    group_match = re.search(r"\b(\d{6,})\b", state.user_input)
    if group_match:
        intent["group_id"] = group_match.group(1)

    count_match = re.search(r"(\d+)\s*(?:个|条|页)", state.user_input)
    if count_match:
        intent["params"]["count"] = int(count_match.group(1))

    state.parsed_intent = intent
    state.log(f"解析结果: {intent}")

    return state
