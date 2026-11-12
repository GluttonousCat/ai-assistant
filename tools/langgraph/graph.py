# -*- encoding: utf-8 -*-
"""
声明式建图: StateGraph 薄封装 — 链式 node/edge/route, 高频图型一键成型,
未覆盖能力经 .graph 逃生舱透传原生对象 (interrupt/Command/send 等直接用原生)。

    from typing import TypedDict
    from langgraph.graph import START, END
    class S(TypedDict):                  # schema 必须显式声明键 (见下方坑)
        n: int
    f = (Flow(S)
         .node("double", lambda s: {"n": s["n"] * 2})
         .node("plus1", lambda s: {"n": s["n"] + 1})
         .chain(START, "double", "plus1", END)
         .compile())
    f.invoke({"n": 3})                   # {"n": 7}

⚠ schema 坑 (langgraph 1.1.3 实证): 必须用显式 TypedDict / MessagesState /
pydantic; 裸 dict schema 下未声明的输入键不进通道 — 节点直通链碰巧可见,
路由 chooser 与 checkpointer 持久化场景全丢 (chooser 收到 {})。

条件路由 (route 的源必须是已声明的节点或 START):
    f = (Flow(dict)
         .node("judge", lambda s: {})     # 传送门节点 (只路由不写状态)
         .node("even", ...).node("odd", ...)
         .edge(START, "judge")
         .route("judge", lambda s: "even" if s["n"] % 2 == 0 else "odd",
                ["even", "odd"])
         .compile())

短期累积键必须带 reducer (实证: 无 reducer 的键不跨轮累积):
    class S(TypedDict):
        seen: Annotated[list, append]    # 每次返回增量, 框架合并进旧值
    app = Flow(S).node("tick", lambda s: {"seen": [s["x"]]}) \\
         .chain(START, "tick", END).compile(checkpointer=ShortTerm().saver)
    invoke(app, {"x": "a"}, thread_id="t1")
    invoke(app, {"x": "b"}, thread_id="t1")   # seen == ["a", "b"]

记忆编译 (checkpointer/store 透传原生参数):
    app = Flow(MessagesState).node(...).compile(
        checkpointer=ShortTerm().saver, store=LongTerm().store)
    invoke(app, {"messages": [...]}, thread_id="t1")   # 同线程共享 state
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

try:                                    # 软导入: 缺 langgraph 时包仍可 import,
    from langgraph.graph import END, START, MessagesState, StateGraph
except ImportError:                     # Flow() 使用时再抛可读错误
    END = START = MessagesState = StateGraph = None

_NO_LG = ("需要 langgraph: .venv/Scripts/python.exe -m pip install langgraph "
          "(宿主项目 .venv 已装, 系统 Python 未装)")

logger = logging.getLogger(__name__)


def append(old: Optional[List], new: Optional[List]) -> List:
    """通用列表累积 reducer (Annotated[list, append]):
    节点返回增量, 框架合并进线程旧值 — 短期记忆自定义键的地基。
    messages 键请用原生 add_messages (消息级去重/ID 合并)。"""
    return (old or []) + (new or [])


def invoke(app, state: Any, thread_id: str = "") -> Any:
    """app.invoke 捷径: 带 thread_id 即启用 checkpointer 短期记忆
    (同线程多次调用共享 state, 异线程互不可见)。"""
    cfg = {"configurable": {"thread_id": thread_id}} if thread_id else None
    return app.invoke(state, config=cfg)


class Flow:
    """链式建图器。每个方法只做一件事并原样转发 StateGraph, 自身不持状态语义。"""

    def __init__(self, schema=MessagesState):
        if StateGraph is None:
            raise ImportError(_NO_LG)
        self._g = StateGraph(schema)

    # ---------- 逃生舱 ----------

    @property
    def graph(self) -> StateGraph:
        """原生 StateGraph — interrupt/Command/send/子图等未封装能力从这里直用"""
        return self._g

    # ---------- 链式声明 ----------

    def node(self, name: str, fn: Callable) -> "Flow":
        """fn(state) -> 部分状态 (返回增量 dict, 由 schema 的 reducer 合并)"""
        self._g.add_node(name, fn)
        return self

    def nodes(self, **named: Callable) -> "Flow":
        """批量: .nodes(double=fn1, plus1=fn2)"""
        for name, fn in named.items():
            self._g.add_node(name, fn)
        return self

    def edge(self, a: str, b: str) -> "Flow":
        self._g.add_edge(a, b)
        return self

    def chain(self, *names: str) -> "Flow":
        """顺序串联: .chain(START, "a", "b", END) == a→b 两跳"""
        for a, b in zip(names, names[1:]):
            self._g.add_edge(a, b)
        return self

    def route(self, src: str, chooser: Callable, targets) -> "Flow":
        """条件路由: chooser(state) -> 目标节点名 (或 END); targets 显式列出
        (先列分支再连向各自的下一跳, 与 langgraph 原生语义一致)"""
        self._g.add_conditional_edges(src, chooser, targets)
        return self

    def compile(self, **kw):
        """编译为可执行 app; kw 透传原生 (checkpointer= / store= 等)"""
        return self._g.compile(**kw)
