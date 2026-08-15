"""
Text-to-SQL 评测运行器
批量执行 1000 条用例, 统计多维指标, 结果留存为 JSON/CSV

评测维度:
- 意图准确率: 识别意图与预期一致
- SQL 生成率: 成功生成可执行 SQL 的比例
- SQL 拦截率: 危险用例被 sql_guard 正确拒绝的比例
- 查询成功率: 执行无异常 (含空结果)
- 数据命中率: 非空结果 (受回填进度影响, 单独统计)
- 响应成功率: 返回非错误响应
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_DIR = Path(__file__).resolve().parent / "results"
OUT_DIR.mkdir(exist_ok=True)

# agent 依赖延迟导入 (main 中 mode 决定 OPENAI_API_KEY 后再触发)
_AGENT_MODULE = None


def _get_agent():
    """懒加载 agent (确保 mode 设置 env 后 import)"""
    global _AGENT_MODULE
    if _AGENT_MODULE is None:
        from agent.fin_graph import invoke_financial_agent
        _AGENT_MODULE = invoke_financial_agent
    return _AGENT_MODULE


def run_case(case: Dict, timeout: float = 30) -> Dict:
    """执行单条用例, 记录多维度结果"""
    t0 = time.time()
    invoke = _get_agent()
    result = {
        "id": case["id"],
        "question": case["question"],
        "type": case["type"],
        "expected_intent": case["expected_intent"],
        "latency_ms": None,
        "actual_intent": None,
        "intent_ok": False,
        "sql": None,
        "sql_generated": False,
        "sql_blocked": False,       # 危险 SQL 被拦截
        "exec_ok": None,            # 执行是否无异常 (None=无SQL)
        "rows": None,
        "has_data": False,          # 非空结果
        "error": None,
        "response": "",
    }
    try:
        state = invoke(case["question"])
        result["latency_ms"] = int((time.time() - t0) * 1000)
        pi = state.parsed_intent or {}
        result["actual_intent"] = pi.get("type")
        result["intent_ok"] = pi.get("type") == case["expected_intent"]

        fr = state.fin_result or {}
        sql = fr.get("sql")
        result["sql"] = sql
        if sql:
            result["sql_generated"] = True

        # 危险 SQL: 来自边缘用例且被拦截 (fin_result None 且 response 含拒绝)
        if case["type"] == "edge" and "DROP" in str(case.get("question", "")).upper():
            result["sql_blocked"] = "SQL 校验" in str(state.response)
        elif state.response and "❌" in state.response:
            result["error"] = state.response
            if sql is None:
                result["sql_blocked"] = True  # 安全措施生效且未生成SQL

        result["rows"] = fr.get("rows")
        result["has_data"] = (fr.get("rows") or 0) > 0
        result["exec_ok"] = not (state.response.startswith("❌"))
        result["response"] = state.response[:500]
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return result


def aggregate(results: List[Dict]) -> Dict:
    """统计评测指标"""
    n = len(results)
    agg: Dict = {"total": n}

    # 意图准确率
    intent_ok = sum(1 for r in results if r["intent_ok"])
    agg["intent_accuracy"] = intent_ok / n if n else 0

    # SQL 生成率 (query 类)
    query_cases = [r for r in results if r["type"] in ("financial", "market", "combined")]
    if query_cases:
        sql_gen = sum(1 for r in query_cases if r["sql_generated"])
        agg["sql_generation_rate"] = sql_gen / len(query_cases)
        # 执行成功率 (有SQL且无异常)
        exec_ok = sum(1 for r in query_cases if r["sql"] and r["exec_ok"])
        agg["execution_success_rate"] = exec_ok / len(query_cases)
        # 数据命中 (非空)
        has_data = sum(1 for r in query_cases if r["has_data"])
        agg["data_hit_rate"] = has_data / len(query_cases)
    else:
        agg["sql_generation_rate"] = 0
        agg["execution_success_rate"] = 0
        agg["data_hit_rate"] = 0

    # 危险拦截
    edge_cases = [r for r in results if r["type"] == "edge"]
    if edge_cases:
        blocked = sum(1 for r in edge_cases
                      if r["sql_blocked"] or r["expected_intent"] != r["actual_intent"])
        agg["edge_handled_rate"] = blocked / len(edge_cases)
    else:
        agg["edge_handled_rate"] = 0

    # 平均延迟
    latencies = [r["latency_ms"] for r in results if r["latency_ms"]]
    agg["avg_latency_ms"] = sum(latencies) / len(latencies) if latencies else 0

    # 按类型细分
    by_type = {}
    for t in set(r["type"] for r in results):
        subset = [r for r in results if r["type"] == t]
        intent_ok_t = sum(1 for r in subset if r["intent_ok"])
        by_type[t] = {
            "count": len(subset),
            "intent_accuracy": intent_ok_t / len(subset),
        }
    agg["by_type"] = by_type
    return agg


def save_results(results: List[Dict], agg: Dict):
    """留存用例与结果"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    # 完整结果 JSON
    with open(OUT_DIR / f"eval_results_{ts}.json", "w", encoding="utf-8") as f:
        json.dump({"meta": agg, "results": results}, f, ensure_ascii=False, indent=2)
    # CSV (可 Excel 打开)
    with open(OUT_DIR / f"eval_results_{ts}.csv", "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "question", "type", "expected_intent", "actual_intent",
            "intent_ok", "sql_generated", "exec_ok", "rows", "has_data",
            "sql_blocked", "latency_ms", "error",
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k) for k in writer.fieldnames})
    return OUT_DIR / f"eval_results_{ts}.csv"


def print_report(agg: Dict):
    print("=" * 60)
    print("                Text-to-SQL 评测报告")
    print("=" * 60)
    print(f"总用例: {agg['total']}")
    print(f"意图准确率:      {agg['intent_accuracy']*100:.1f}%")
    print(f"SQL 生成率:      {agg['sql_generation_rate']*100:.1f}%  (query类)")
    print(f"执行成功率:      {agg['execution_success_rate']*100:.1f}%")
    print(f"数据命中率:      {agg['data_hit_rate']*100:.1f}%  (受回填进度影响)")
    print(f"边界处理率:      {agg['edge_handled_rate']*100:.1f}%")
    print(f"平均延迟:        {agg['avg_latency_ms']:.0f}ms")
    print("-" * 60)
    print("按类型:")
    for t, d in agg.get("by_type", {}).items():
        print(f"  {t:12s}: {d['count']:4d} 条, 意图准确 {d['intent_accuracy']*100:.1f}%")
    print("=" * 60)


def main(n: int = 1000, save: bool = True, mode: str = "rule"):
    """
    mode:
      'rule' - 禁用 LLM, 只测规则路径 (无配额限制, 快速)
      'llm'  - 启用 LLM, 测完整链路 (需有效 API key)
    """
    from evals.generate_cases import generate_cases

    print(f"生成 {n} 条评测用例... (mode={mode})")
    cases = generate_cases(n)
    print(f"开始评测 ({n} 条)...")
    t0 = time.time()

    results = []
    for i, case in enumerate(cases, 1):
        r = run_case(case)
        results.append(r)
        if i % 100 == 0:
            elapsed = time.time() - t0
            print(f"  已执行 {i}/{n}, 耗时 {elapsed:.0f}s")

    agg = aggregate(results)
    print_report(agg)

    if save:
        paths = save_results(results, agg)
        print(f"\n结果已留存: {paths}")
    return agg, results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Text-to-SQL 评测")
    parser.add_argument("--n", type=int, default=1000, help="用例数 (默认1000)")
    parser.add_argument("--mode", type=str, default="rule",
                        choices=["rule", "llm"],
                        help="rule=禁用LLM只测规则路径, llm=完整链路")
    args = parser.parse_args()

    if args.mode == "rule":
        import os
        os.environ["OPENAI_API_KEY"] = ""
    main(n=args.n, mode=args.mode)