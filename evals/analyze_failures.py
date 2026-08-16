"""分析评测失败样例 (attack / compare / edge)"""
import json
from pathlib import Path

files = sorted(Path("evals/results").glob("eval_results_*.json"))
result = json.loads(files[-1].read_text(encoding="utf-8"))
results = result["results"]
print(f"分析文件: {files[-1].name}, 共 {len(results)} 条\n")

print("=== attack 类型 (预期拦截, 实际未识别) ===")
shown = 0
for r in results:
    if r["type"] == "attack" and not r["intent_ok"]:
        print(f'  Q: {r["question"][:70]}')
        print(f'    期望={r["expected_intent"]} 实际={r["actual_intent"]} '
              f'sql_gen={r["sql_generated"]} blocked={r["sql_blocked"]}')
        shown += 1
        if shown >= 10:
            break
print(f"(共展示 {shown} 条)")

print("\n=== compare 失败样例 ===")
shown = 0
for r in results:
    if r["type"] == "compare" and not r["intent_ok"]:
        print(f'  Q: {r["question"][:60]}  实际={r["actual_intent"]}')
        shown += 1
        if shown >= 10:
            break

print("\n=== edge 失败样例 ===")
shown = 0
for r in results:
    if r["type"] == "edge" and not r["intent_ok"]:
        print(f'  Q: {r["question"][:70]}  期望={r["expected_intent"]} 实际={r["actual_intent"]}')
        shown += 1
        if shown >= 8:
            break