# -*- encoding: utf-8 -*-
"""
【试验田】LangGraph 版 interpret→ppt 链路 — 与 cli.py 直连函数链的对照实验

目的: 验证"业务流程用 Graph 编排是否更整洁"。与 cli.py 的 interpret 子命令
调用**完全相同**的底层函数 (write_stock_article / generate_profile_ppt),
只比较编排层差异。节点边界 = 业务步骤; 步骤内部仍是普通函数/LLM 调用
(agent-loop-as-node 的形态示范)。

用法 (与 cli.py interpret 等价):
    python -m skills.content.graph_spike 中际旭创 --ppt
    python -m skills.content.graph_spike 中际旭创

实验结论 (2026-09-14, 见 graph 底部统计): 线性链路上图编排代码量约 3 倍、
间接层 +1; 价值出现在分支/并行/检查点恢复/审批中断需求出现时。
此文件保留作为"图+节点"模式参考, 无生产依赖。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


# ---------- 图状态: 节点间传递的全部业务数据 ----------
class InterpretState(TypedDict, total=False):
    stock: str
    years: int
    annual_year: Optional[int]
    use_annual: bool
    do_ppt: bool
    article: Dict[str, Any]
    ppt: Dict[str, Any]
    error: str


# ---------- 节点: 每个节点 = 一个业务步骤, 返回增量状态 ----------
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
