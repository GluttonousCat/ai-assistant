# -*- encoding: utf-8 -*-
"""langgraph atomic smoke tests (no network / no LLM; whole group skips when
langgraph is missing)

Covers four empirical findings (langgraph 1.1.3):
① with an explicit TypedDict schema, nodes/routing/persistence all pass
② reducer keys (append) accumulate across turns within a thread; messages
  uses the native add_messages
③ cross-thread isolation + reset-based forgetting
④ long-term memory namespace read/write/forget/isolation
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
    seen: Annotated[list, append]       # reducer key: foundation of short-term accumulation


def test_flow_chain():
    f = (Flow(_Calc)
         .nodes(double=lambda s: {"n": s["n"] * 2},
                plus1=lambda s: {"n": s["n"] + 1})
         .edge(START, "double")
         .chain("double", "plus1", END))
    assert f.compile().invoke({"n": 3})["n"] == 7


def test_flow_route():
    f = (Flow(_Route)
         .nodes(judge=lambda s: {},                     # portal: routes only, writes no state
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
    """Short-term-memory atoms: same-thread accumulation (reducer key
    required) / cross-thread isolation / reset forgetting"""
    st = ShortTerm()
    app = (Flow(_Acc)
           .node("tick", lambda s: {"seen": [s["x"]]})  # returns only a delta
           .chain(START, "tick", END)
           .compile(checkpointer=st.saver))
    invoke(app, {"x": "a"}, thread_id="t1")
    invoke(app, {"x": "b"}, thread_id="t1")
    invoke(app, {"x": "z"}, thread_id="t2")
    assert st.state(app, "t1")["seen"] == ["a", "b"]      # reducer accumulation
    assert st.state(app, "t2")["seen"] == ["z"]           # thread isolation
    st.reset("t1")
    assert st.state(app, "t1") is None                    # forgetting


def test_short_term_messages_history():
    """Reading thread history for the messages key (native add_messages)"""
    st = ShortTerm()
    app = (Flow(MessagesState)
           .node("echo", lambda s: {"messages": [AIMessage("ok")]})
           .chain(START, "echo", END)
           .compile(checkpointer=st.saver))
    invoke(app, {"messages": [HumanMessage("hi")]}, thread_id="t1")
    invoke(app, {"messages": [HumanMessage("again")]}, thread_id="t1")
    texts = [str(m.content) for m in st.messages(app, "t1")]
    assert texts == ["hi", "ok", "again", "ok"]           # full conversation history


def test_long_term_namespace_roundtrip():
    lt = LongTerm()
    lt.remember(("user", "alice"), "profile", {"likes": "猫"})
    lt.remember(("user", "alice"), "job", {"role": "工程师"})
    got = lt.recall(("user", "alice"))
    assert {"key": "profile", "likes": "猫"} in got
    lt.forget(("user", "alice"), "job")
    assert all(m["key"] != "job" for m in lt.recall(("user", "alice")))
    assert lt.recall(("user", "bob")) == []               # namespace isolation
