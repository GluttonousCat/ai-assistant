# -*- encoding: utf-8 -*-
"""langgraph 原子冒烟测试 (无网络/无 LLM; 缺 langgraph 时整组 skip)

覆盖四条实证结论 (langgraph 1.1.3):
① 显式 TypedDict schema 下节点/路由/持久化全通
② reducer 键 (append) 同线程跨轮累积; messages 走原生 add_messages
③ 异线程隔离 + reset 遗忘
④ 长期记忆命名空间读写/遗忘/隔离
"""
from __future__ import annotations

from typing import Annotated, TypedDict

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState

from tools.langgraph.graph import Flow, append, invoke
from tools.langgraph.memory import LongTerm, ShortTerm


class _Calc(TypedDict):
    n: int


class _Route(TypedDict):
    n: int
    tag: str


class _Acc(TypedDict):
    x: str
    seen: Annotated[list, append]       # reducer 键: 短期累积的地基


def test_flow_chain():
    f = (Flow(_Calc)
         .nodes(double=lambda s: {"n": s["n"] * 2},
                plus1=lambda s: {"n": s["n"] + 1})
         .edge(START, "double")
         .chain("double", "plus1", END))
    assert f.compile().invoke({"n": 3})["n"] == 7


def test_flow_route():
    f = (Flow(_Route)
         .nodes(judge=lambda s: {},                     # 传送门: 只路由不写状态
                even=lambda s: {"tag": "偶"},
                odd=lambda s: {"tag": "奇"})
         .edge(START, "judge")
         .route("judge", lambda s: "even" if s["n"] % 2 == 0 else "odd",
                ["even", "odd"])
         .edge("even", END).edge("odd", END)
         .compile())
    assert f.invoke({"n": 4})["tag"] == "偶"
    assert f.invoke({"n": 5})["tag"] == "奇"


def test_short_term_accumulation():
    """短期记忆原子: 同线程累积 (需 reducer 键) / 异线程隔离 / reset 遗忘"""
    st = ShortTerm()
    app = (Flow(_Acc)
           .node("tick", lambda s: {"seen": [s["x"]]})  # 只返回增量
           .chain(START, "tick", END)
           .compile(checkpointer=st.saver))
    invoke(app, {"x": "a"}, thread_id="t1")
    invoke(app, {"x": "b"}, thread_id="t1")
    invoke(app, {"x": "z"}, thread_id="t2")
    assert st.state(app, "t1")["seen"] == ["a", "b"]      # reducer 累积
    assert st.state(app, "t2")["seen"] == ["z"]           # 线程隔离
    st.reset("t1")
    assert st.state(app, "t1") is None                    # 遗忘


def test_short_term_messages_history():
    """messages 键 (原生 add_messages) 的线程历史读取"""
    st = ShortTerm()
    app = (Flow(MessagesState)
           .node("echo", lambda s: {"messages": [AIMessage("ok")]})
           .chain(START, "echo", END)
           .compile(checkpointer=st.saver))
    invoke(app, {"messages": [HumanMessage("hi")]}, thread_id="t1")
    invoke(app, {"messages": [HumanMessage("again")]}, thread_id="t1")
    texts = [str(m.content) for m in st.messages(app, "t1")]
    assert texts == ["hi", "ok", "again", "ok"]           # 对话历史完整


def test_long_term_namespace_roundtrip():
    lt = LongTerm()
    lt.remember(("user", "alice"), "profile", {"likes": "猫"})
    lt.remember(("user", "alice"), "job", {"role": "工程师"})
    got = lt.recall(("user", "alice"))
    assert {"key": "profile", "likes": "猫"} in got
    lt.forget(("user", "alice"), "job")
    assert all(m["key"] != "job" for m in lt.recall(("user", "alice")))
    assert lt.recall(("user", "bob")) == []               # 命名空间隔离
