# -*- encoding: utf-8 -*-
"""
Memory abstractions: short-term = thread (checkpointer) / long-term =
cross-thread namespace (Store).

LangGraph's two memory primitives (matching its official checkpointer /
store concepts):
- ShortTerm: multiple invokes with the same thread_id share state; a new
  thread_id is a new session; reset clears a single thread. Backends:
  memory (in-process) / sqlite (persistent).
  ⚠ Accumulation works only for reducer keys (empirical, langgraph 1.1.3):
  messages carries add_messages natively; custom keys must be
  Annotated[list, append] (graph.append); plain keys of a dict schema do
  not accumulate across turns.
- LongTerm: key-value entries inside a namespace tuple, readable/writable
  from any thread; recall supports semantic retrieval (when the backend
  supports it; InMemoryStore is keyword/full-scan). R&D use case: facts
  distilled from conversations → recalled across sessions, i.e. the
  minimal kernel of "user profile / preference memory".

    st = ShortTerm()                                  # in-process
    st = ShortTerm.sqlite("mem.db")                   # persistent via SqliteSaver
    lt = LongTerm()
    lt.remember(("user", "alice"), "profile", {"likes": "cats"})
    lt.recall(("user", "alice"))                      # [{"key": "profile", "likes": "cats"}]

Wiring into a graph: Flow(...).compile(checkpointer=st.saver, store=lt.store)
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Dict, List, Tuple

try:                                    # soft import: without langgraph the package still imports,
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
except ImportError:                     # constructors raise a readable error at use time (cf. graph._NO_LG)
    InMemorySaver = InMemoryStore = None

_NO_LG = ("需要 langgraph: .venv/Scripts/python.exe -m pip install langgraph "
          "(宿主项目 .venv 已装, 系统 Python 未装)")

NS = Tuple[str, ...]   # namespace: hierarchical tuple, e.g. ("user", "alice")


class ShortTerm:
    """Thread-scoped short-term memory (checkpointer wrapper)"""

    def __init__(self, saver=None):
        if InMemorySaver is None:
            raise ImportError(_NO_LG)
        self.saver = saver or InMemorySaver()

    @classmethod
    @contextmanager
    def sqlite(cls, path: str):
        """Persistent via SqliteSaver (requires langgraph-checkpoint-sqlite):
        with ShortTerm.sqlite("mem.db") as st: ...   (connection closes with the context)"""
        from langgraph.checkpoint.sqlite import SqliteSaver
        with SqliteSaver.from_conn_string(path) as saver:
            yield cls(saver)

    def config(self, thread_id: str) -> Dict:
        return {"configurable": {"thread_id": thread_id}}

    def state(self, app, thread_id: str):
        """Current thread state (values includes accumulated messages); None for an empty thread"""
        snap = app.get_state(self.config(thread_id))
        return snap.values if snap and snap.values else None

    def messages(self, app, thread_id: str) -> List:
        """Thread message history (requires the graph schema to have a messages key, e.g. MessagesState)"""
        st = self.state(app, thread_id) or {}
        return st.get("messages", [])

    def reset(self, thread_id: str) -> None:
        """Clear a single thread (short-term-memory forgetting)"""
        self.saver.delete_thread(thread_id)


class LongTerm:
    """Cross-thread long-term memory (Store wrapper): namespaced key-value entries shared by all threads"""

    def __init__(self, store=None):
        if InMemoryStore is None:
            raise ImportError(_NO_LG)
        self.store = store or InMemoryStore()

    def remember(self, ns: NS, key: str, value: Dict) -> None:
        """Write/overwrite one memory entry (value must be a dict — Store contract)"""
        self.store.put(ns, key, value)

    def recall(self, ns: NS, query: str = "", limit: int = 8) -> List[Dict]:
        """Search within a namespace: empty query = everything; a query goes
        through the backend's retrieval (InMemoryStore: keyword substring;
        vector backends: semantic).
        Returns [{"key": ..., **value}] ordered by relevance."""
        items = self.store.search(ns, query=query or None, limit=limit)
        return [{"key": it.key, **it.value} for it in items]

    def forget(self, ns: NS, key: str) -> None:
        self.store.delete(ns, key)
