# -*- encoding: utf-8 -*-
"""
LangGraph 原子能力封装 (仅原子, 无功能落地)

定位: tools 层只封装 LangGraph 的原子能力供项目各处组合 —
图编排 (StateGraph) / 短期记忆 (checkpointer, 线程内) / 长期记忆
(Store, 跨线程命名空间)。**具体功能实现 (记忆实验台、工作流应用等)
放项目其他模块**, 本包不装任何应用图。**不替代 harness**: 主对话循环
仍是手写三级漏斗 (AGENTS.md), 本包用于新范式研发的积木。

设计约定 (与 tools/cninfo 同源):
- 薄封装 + 逃生舱: 只封装高频样板, 未覆盖能力经 .graph 透传原生
  StateGraph (interrupt/Command/send/子图直用原生); compile(**kw)
  透传 checkpointer/store 等原生参数
- 零项目依赖: 仅依赖 langgraph + 标准库, 整目录可独立拷出
- LLM 归调用方: 本包不绑定任何模型客户端

关键实证 (langgraph 1.1.3): **无 reducer 的键不跨轮累积** — dict schema
下第二次 invoke 节点读不到第一次写入的普通键 (LastValue 输入即断链);
短期累积必须用 reducer 键 (messages 用原生 add_messages, 自定义列表键
用本包 append)。见 tests/test_smoke.py::test_short_term_accumulation。

结构:
- graph.py     Flow 链式建图 (node/edge/route/chain) + append reducer
               + invoke 线程捷径
- memory.py    ShortTerm (checkpointer 线程记忆) / LongTerm (Store 命名空间记忆)
- tests/       原子冒烟测试 (缺 langgraph 时整组 skip)

依赖: langgraph (已在 .venv); 可选 langgraph-checkpoint-sqlite (SqliteSaver)
"""
from .graph import Flow, append, invoke
from .memory import LongTerm, ShortTerm

__all__ = ["Flow", "invoke", "append", "ShortTerm", "LongTerm"]
