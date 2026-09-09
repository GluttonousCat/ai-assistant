# -*- encoding: utf-8 -*-
"""
Agent 对话评测器 (v1)

对 evals/agent_cases.py 全量用例跑真实 LLM+PG, 断言:
工具选择 / 回答关键词 / 无工具泄漏, 输出 JSON 报告 + 控制台摘要。

用法 (项目根目录):
    .venv\\Scripts\\python.exe evals\\run_agent_eval.py                  # 全量 (快车道开=线上路径)
    .venv\\Scripts\\python.exe evals\\run_agent_eval.py --no-fast-lane  # 强制全走 Agent 循环
    .venv\\Scripts\\python.exe evals\\run_agent_eval.py --only multiturn,query  # 按类型过滤

报告: evals/results/agent_eval_<ts>.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.agent_cases import AGENT_CASES, GLOBAL_NOT_CONTAINS  # noqa: E402


def check_turn(turn: dict, tools: list, answer: str) -> tuple:
    """单轮断言 -> (ok, failures[list of str])"""
    failures = []

    called = [t.get("tool", "") for t in tools]
    if turn.get("expect_no_tools") and called:
        failures.append(f"不应调工具, 实际调了: {called}")
    if turn.get("tools_any") and not set(turn["tools_any"]) & set(called):
        failures.append(f"工具未命中 {turn['tools_any']}, 实际: {called}")
    if turn.get("tools_none") and set(turn["tools_none"]) & set(called):
        failures.append(f"禁用工具被调用: "
                        f"{set(turn['tools_none']) & set(called)}")
    if turn.get("contains_any") and not any(
            k in answer for k in turn["contains_any"]):
        failures.append(f"回答缺少关键词之一 {turn['contains_any']}")
    if turn.get("contains_all"):
        missing = [k for k in turn["contains_all"] if k not in answer]
        if missing:
            failures.append(f"回答缺少关键词 {missing}")
    for bad in list(turn.get("not_contains", [])) + GLOBAL_NOT_CONTAINS:
        if bad in answer:
            failures.append(f"回答出现禁词: {bad}")
    if not answer.strip():
        failures.append("回答为空")
    return (not failures, failures)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fast-lane", action="store_true",
                        help="强制全走 Agent 循环 (默认快车道开 = 线上路径)")
    parser.add_argument("--only", default="",
                        help="按 case id / type 子串过滤, 逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="最多跑 N 个用例")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from agent.loop import run_agent

    filters = [f.strip().lower() for f in args.only.split(",") if f.strip()]
    cases = [c for c in AGENT_CASES
             if not filters or any(f in c["id"].lower() or f in c["type"].lower()
                                   for f in filters)]
    if args.limit:
        cases = cases[:args.limit]

    fast_lane = not args.no_fast_lane
    print(f"== Agent eval: {len(cases)} cases, fast_lane={fast_lane} ==")

    results, n_turns, n_pass_turns, n_pass_cases = [], 0, 0, 0
    for c in cases:
        case_ok = True
        sid = f"eval-{c['id']}"
        for i, turn in enumerate(c["turns"], 1):
            res = run_agent(turn["q"], session_id=sid, fast_lane=fast_lane)
            tools = res["tool_events"]
            answer = res["answer"] or ""
            ok, failures = check_turn(turn, tools, answer)
            if res.get("error"):
                failures.append(f"运行错误: {res['error']}")
                ok = False
            n_turns += 1
            n_pass_turns += int(ok)
            case_ok = case_ok and ok
            results.append({
                "case_id": c["id"], "type": c["type"], "turn": i,
                "question": turn["q"], "ok": ok, "failures": failures,
                "tools": [t.get("tool") for t in tools],
                "answer_head": answer[:200],
            })
            mark = "✓" if ok else "✗"
            print(f"  {mark} [{c['type']:9s}] {c['id']}#{i} "
                  f"tools={[t.get('tool') for t in tools]}")
            for f in failures:
                print(f"      - {f}")
        n_pass_cases += int(case_ok)

    # ---- 报告 ----
    by_type: dict = {}
    for r in results:
        stat = by_type.setdefault(r["type"], {"turns": 0, "pass": 0})
        stat["turns"] += 1
        stat["pass"] += int(r["ok"])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("evals/results") / f"agent_eval_{ts}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "ts": ts, "fast_lane": fast_lane,
        "summary": {"cases": len(cases), "cases_pass": n_pass_cases,
                    "turns": n_turns, "turns_pass": n_pass_turns,
                    "turn_pass_rate": round(n_pass_turns / n_turns * 100, 1)
                    if n_turns else 0,
                    "by_type": by_type},
        "results": results,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n== 轮次通过率: {n_pass_turns}/{n_turns} "
          f"({n_pass_turns / n_turns * 100:.0f}%), 用例通过: {n_pass_cases}/{len(cases)} ==")
    for t, s in sorted(by_type.items()):
        print(f"  {t:10s} {s['pass']}/{s['turns']}")
    print(f"报告: {out}")
    return 0 if n_pass_turns == n_turns else 1


if __name__ == "__main__":
    sys.exit(main())
