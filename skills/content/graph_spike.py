# -*- encoding: utf-8 -*-
"""
【Experimental sandbox】LangGraph version of the interpret->ppt pipeline — a
controlled experiment against the direct function chain in cli.py

Purpose: verify whether "orchestrating business flows with a Graph" is cleaner.
Calls the **exact same** underlying functions as cli.py's interpret subcommand
(write_stock_article / generate_profile_ppt), comparing only the orchestration
layer. Node boundaries = business steps; inside a step it is still plain
function/LLM calls (a demonstration of the agent-loop-as-node shape).

Usage (equivalent to cli.py interpret):
    python -m skills.content.graph_spike 中际旭创 --ppt
    python -m skills.content.graph_spike 中际旭创

Experiment conclusion (2026-09-14, see the stats at the bottom of graph): on a
linear pipeline the graph orchestration is ~3x the code with +1 indirection
layer; the value shows up once branching/parallelism/checkpoint recovery/
approval interrupts are needed.
Kept as a "graph + node" pattern reference; no production dependency.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


# ---------- Graph state: all business data passed between nodes ----------
class InterpretState(TypedDict, total=False):
    stock: str
    years: int
    annual_year: Optional[int]
    use_annual: bool
    do_ppt: bool
    article: Dict[str, Any]
    ppt: Dict[str, Any]
    error: str


# ---------- Nodes: each node = one business step, returns incremental state ----------
def prepare(state: InterpretState) -> dict:
    if not (state.get("stock") or "").strip():
        return {"error": "stock 不能为空"}
    return {}


def write_article(state: InterpretState) -> dict:
    if state.get("error"):
        return {}
    from skills.content.article import write_stock_article
    res = write_stock_article(state["stock"], state.get("years", 5),
                              annual_year=state.get("annual_year") or None,
                              use_annual=state.get("use_annual", True))
    return {"article": res}


def make_ppt(state: InterpretState) -> dict:
    if state.get("error"):
        return {}
    try:
        from skills.content.ppt import generate_profile_ppt
        return {"ppt": generate_profile_ppt(state["stock"])}
    except Exception as e:  # noqa: BLE001
        return {"ppt": {"ok": False, "error": str(e)}}


def route_after_article(state: InterpretState) -> str:
    if state.get("error"):
        return END
    return "ppt" if state.get("do_ppt") else END


def build_graph():
    g = StateGraph(InterpretState)
    g.add_node("prepare", prepare)
    g.add_node("article", write_article)
    g.add_node("ppt", make_ppt)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "article")
    g.add_conditional_edges("article", route_after_article, {"ppt": "ppt", END: END})
    g.add_edge("ppt", END)
    return g.compile()


def main() -> int:
    p = argparse.ArgumentParser(description="interpret 链路 (LangGraph 试验田)")
    p.add_argument("stock")
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--annual-year", type=int, default=0)
    p.add_argument("--no-annual", action="store_true")
    p.add_argument("--ppt", action="store_true")
    args = p.parse_args()

    graph = build_graph()
    final = graph.invoke({
        "stock": args.stock, "years": args.years,
        "annual_year": args.annual_year or None,
        "use_annual": not args.no_annual, "do_ppt": args.ppt,
    })

    if final.get("error"):
        print("ERROR:", final["error"])
        return 1
    art = final.get("article") or {}
    print(json.dumps({k: v for k, v in art.items() if k != "preview"},
                     ensure_ascii=False, indent=1))
    print("\n---- 预览 ----\n", art.get("preview", ""))
    if final.get("ppt"):
        ppt = final["ppt"]
        print(json.dumps({k: ppt[k] for k in ("ok", "pptx", "error") if k in ppt},
                         ensure_ascii=False, indent=1))
        return 0 if ppt.get("ok") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
