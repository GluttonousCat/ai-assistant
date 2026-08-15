"""
响应生成节点
"""


def generate_response_node(state) -> object:
    """生成最终回复"""
    if not state.response:
        state.response = "处理完成"
    return state
