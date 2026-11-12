# -*- encoding: utf-8 -*-
"""
Declarative graph building: thin StateGraph wrapper — chained
node/edge/route, high-frequency graph shapes in one go; uncovered
capabilities pass through the .graph escape hatch to native objects
(interrupt/Command/send etc. used natively).

    from typing import TypedDict
    from langgraph.graph import START, END
    class S(TypedDict):                  # schema keys must be declared explicitly (pitfall below)
        n: int
    f = (Flow(S)
         .node("double", lambda s: {"n": s["n"] * 2})
         .node("plus1", lambda s: {"n": s["n"] + 1})
         .chain(START, "double", "plus1", END)
         .compile())
    f.invoke({"n": 3})                   # {"n": 7}

⚠ Schema pitfall (empirical, langgraph 1.1.3): use an explicit TypedDict /
MessagesState / pydantic schema; under a bare dict schema undeclared input
keys never enter a channel — they happen to remain visible along a plain
node chain, but routing choosers and checkpointer persistence lose them
entirely (the chooser receives {}).

Conditional routing (route's source must be a declared node or START):
    f = (Flow(dict)
         .node("judge", lambda s: {})     # portal node (routes only, writes no state)
         .node("even", ...).node("odd", ...)
         .edge(START, "judge")
         .route("judge", lambda s: "even" if s["n"] % 2 == 0 else "odd",
                ["even", "odd"])
         .compile())

Short-term accumulation keys must carry a reducer (empirical: keys without
a reducer do not accumulate across turns):
    class S(TypedDict):
        seen: Annotated[list, append]    # each turn returns a delta; the framework merges it into the old value
    app = Flow(S).node("tick", lambda s: {"seen": [s["x"]]}) \\
         .chain(START, "tick", END).compile(checkpointer=ShortTerm().saver)
    invoke(app, {"x": "a"}, thread_id="t1")
    invoke(app, {"x": "b"}, thread_id="t1")   # seen == ["a", "b"]

Memory-aware compilation (checkpointer/store forwarded natively):
    app = Flow(MessagesState).node(...).compile(
        checkpointer=ShortTerm().saver, store=LongTerm().store)
    invoke(app, {"messages": [...]}, thread_id="t1")   # same thread shares state
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

try:                                    # soft import: without langgraph the package still imports,
    from langgraph.graph import END, START, MessagesState, StateGraph
except ImportError:                     # Flow() raises a readable error at use time
    END = START = MessagesState = StateGraph = None

_NO_LG = ("需要 langgraph: .venv/Scripts/python.exe -m pip install langgraph "
          "(宿主项目 .venv 已装, 系统 Python 未装)")


def append(old: Optional[List], new: Optional[List]) -> List:
    """Generic list-accumulation reducer (Annotated[list, append]):
    nodes return deltas and the framework merges them into the thread's old
    value — the foundation for custom short-term-memory keys.
    Use the native add_messages for the messages key (message-level
    dedup/ID merging)."""
    return (old or []) + (new or [])


def invoke(app, state: Any, thread_id: str = "") -> Any:
    """Shortcut for app.invoke: a thread_id enables checkpointer-backed
    short-term memory (calls in the same thread share state; other threads
    cannot see it)."""
    cfg = {"configurable": {"thread_id": thread_id}} if thread_id else None
    return app.invoke(state, config=cfg)


class Flow:
    """Chained graph builder. Each method does exactly one thing and
    forwards verbatim to StateGraph; the builder itself carries no state
    semantics."""

    def __init__(self, schema=MessagesState):
        if StateGraph is None:
            raise ImportError(_NO_LG)
        self._g = StateGraph(schema)

    # ---------- escape hatch ----------

    @property
    def graph(self) -> StateGraph:
        """Native StateGraph — use unwrapped capabilities (interrupt/Command/send/subgraphs) directly through this"""
        return self._g

    # ---------- chained declaration ----------

    def node(self, name: str, fn: Callable) -> "Flow":
        """fn(state) -> partial state (returns a delta dict, merged by the schema's reducers)"""
        self._g.add_node(name, fn)
        return self

    def nodes(self, **named: Callable) -> "Flow":
        """Batch form: .nodes(double=fn1, plus1=fn2)"""
        for name, fn in named.items():
            self._g.add_node(name, fn)
        return self

    def edge(self, a: str, b: str) -> "Flow":
        self._g.add_edge(a, b)
        return self

    def chain(self, *names: str) -> "Flow":
        """Sequential chaining: .chain(START, "a", "b", END) == the two hops a→b"""
        for a, b in zip(names, names[1:]):
            self._g.add_edge(a, b)
        return self

    def route(self, src: str, chooser: Callable, targets) -> "Flow":
        """Conditional routing: chooser(state) -> target node name (or END);
        targets listed explicitly (declare the branches first, then connect
        each to its next hop — same semantics as native langgraph)"""
        self._g.add_conditional_edges(src, chooser, targets)
        return self

    def compile(self, **kw):
        """Compile into an executable app; kw forwards natively (checkpointer= / store= etc.)"""
        return self._g.compile(**kw)
