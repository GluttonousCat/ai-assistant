# -*- encoding: utf-8 -*-
"""
记忆抽象: 短期 = 线程 (checkpointer) / 长期 = 跨线程命名空间 (Store)

LangGraph 的两类记忆原语 (对应其官方 checkpointer / store 概念):
- ShortTerm: 同一 thread_id 的多次 invoke 共享 state; 换 thread_id 即新会话;
  reset 清空单线程。后端 memory(进程内) / sqlite(持久)。
  ⚠ 累积只对 reducer 键生效 (实证 langgraph 1.1.3): messages 天然带
  add_messages; 自定义键必须 Annotated[list, append] (graph.append),
  dict schema 的普通键不跨轮累积。
- LongTerm: 命名空间元组内的键值条目, 任意线程可读写; recall 支持
  语义检索 (后端支持时, InMemoryStore 为关键词/全量)。R&D 场景:
  对话沉淀事实 → 跨会话召回, 即"用户画像/偏好记忆"的最小内核。

    st = ShortTerm()                                  # 进程内
    st = ShortTerm.sqlite("mem.db")                   # SqliteSaver 持久化
    lt = LongTerm()
    lt.remember(("user", "alice"), "profile", {"likes": "猫"})
    lt.recall(("user", "alice"))                      # [{"key": "profile", "likes": "猫"}]

与图对接: Flow(...).compile(checkpointer=st.saver, store=lt.store)
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

try:                                    # 软导入: 缺 langgraph 时包仍可 import,
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
except ImportError:                     # 构造时再抛可读错误 (同 graph._NO_LG)
    InMemorySaver = InMemoryStore = None

_NO_LG = ("需要 langgraph: .venv/Scripts/python.exe -m pip install langgraph "
          "(宿主项目 .venv 已装, 系统 Python 未装)")

logger = logging.getLogger(__name__)

NS = Tuple[str, ...]   # 命名空间: 层级元组, 如 ("user", "alice")


class ShortTerm:
    """线程短期记忆 (checkpointer 封装)"""

    def __init__(self, saver=None):
        if InMemorySaver is None:
            raise ImportError(_NO_LG)
        self.saver = saver or InMemorySaver()

    @classmethod
    @contextmanager
    def sqlite(cls, path: str):
        """SqliteSaver 持久化 (需 langgraph-checkpoint-sqlite):
        with ShortTerm.sqlite("mem.db") as st: ...   (连接随上下文关闭)"""
        from langgraph.checkpoint.sqlite import SqliteSaver
        with SqliteSaver.from_conn_string(path) as saver:
            yield cls(saver)

    def config(self, thread_id: str) -> Dict:
        return {"configurable": {"thread_id": thread_id}}

    def state(self, app, thread_id: str):
        """线程当前 state (values 含累积 messages); 空线程返回 None"""
        snap = app.get_state(self.config(thread_id))
        return snap.values if snap and snap.values else None

    def messages(self, app, thread_id: str) -> List:
        """线程消息历史 (需图的 schema 含 messages 键, 如 MessagesState)"""
        st = self.state(app, thread_id) or {}
        return st.get("messages", [])

    def reset(self, thread_id: str) -> None:
        """清空单线程 (短期记忆遗忘)"""
        self.saver.delete_thread(thread_id)


class LongTerm:
    """跨线程长期记忆 (Store 封装): 命名空间键值条目, 全线程共享"""

    def __init__(self, store=None):
        if InMemoryStore is None:
            raise ImportError(_NO_LG)
        self.store = store or InMemoryStore()

    def remember(self, ns: NS, key: str, value: Dict) -> None:
        """写/覆盖一条记忆 (value 必须是 dict — Store 契约)"""
        self.store.put(ns, key, value)

    def recall(self, ns: NS, query: str = "", limit: int = 8) -> List[Dict]:
        """命名空间内检索: query 空=全量; 有 query 走后端检索
        (InMemoryStore 关键词子串; 向量后端为语义)。
        返回 [{"key": ..., **value}] 按相关度序。"""
        items = self.store.search(ns, query=query or None, limit=limit)
        return [{"key": it.key, **it.value} for it in items]

    def forget(self, ns: NS, key: str) -> None:
        self.store.delete(ns, key)
