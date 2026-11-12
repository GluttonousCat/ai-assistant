# -*- encoding: utf-8 -*-
"""
LangGraph atomic-capability wrappers (atoms only, no application features)

Positioning: this tools package only wraps LangGraph's atomic capabilities
for composition elsewhere in the project — graph orchestration (StateGraph) /
short-term memory (checkpointer, thread-scoped) / long-term memory (Store,
cross-thread namespaces). **Concrete feature implementations (memory
playground, workflow apps, etc.) live in other project modules**; this
package ships no application graph. **It does not replace harness**: the
main chat loop remains the hand-written three-stage funnel (AGENTS.md);
this package is the building block kit for new-paradigm R&D.

Design conventions (same lineage as tools/cninfo):
- Thin wrapper + escape hatch: wrap only high-frequency boilerplate;
  uncovered capabilities pass through via .graph to the native StateGraph
  (interrupt/Command/send/subgraphs used natively); compile(**kw) forwards
  native params such as checkpointer/store
- Zero project dependencies: only langgraph + stdlib; the whole directory
  can be copied out standalone
- LLM belongs to the caller: this package binds no model client

Key empirical finding (langgraph 1.1.3): **keys without a reducer do not
accumulate across turns** — under a dict schema, on the second invoke nodes
cannot read plain keys written by the first one (LastValue channels break
at input); short-term accumulation must use reducer keys (native
add_messages for messages; this package's append for custom list keys).
See tests/test_smoke.py::test_short_term_accumulation.

Structure:
- graph.py     Flow chained graph builder (node/edge/route/chain) + append
               reducer + invoke thread shortcut
- memory.py    ShortTerm (checkpointer thread memory) / LongTerm (Store
               namespace memory)
- tests/       atomic smoke tests (whole group skips when langgraph is
               missing)

Dependencies: langgraph (already in .venv); optional
langgraph-checkpoint-sqlite (SqliteSaver)
"""
from .graph import Flow, append, invoke
from .memory import LongTerm, ShortTerm

__all__ = ["Flow", "invoke", "append", "ShortTerm", "LongTerm"]
