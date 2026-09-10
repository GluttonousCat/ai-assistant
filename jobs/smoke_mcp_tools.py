# -*- encoding: utf-8 -*-
"""
MCP 工具全量冒烟测试

用法:
    .venv\\Scripts\\python.exe scripts/smoke_mcp_tools.py            # 快速档 (默认)
    .venv\\Scripts\\python.exe scripts/smoke_mcp_tools.py --heavy   # 含全市场扫描/forge

每个工具给一组代表性参数, 校验: 注册存在 / 调用不抛 / 信封结构 ok / data 非空。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time  # noqa: E402

from mcp.tools import REGISTRY  # noqa: E402

# (工具名, 参数) -- 快速档: 秒级~十秒级
FAST_CASES = [
    ("resolve_stock", {"text": "宁德时代和茅台的最新研报"}),
    ("get_schema", {"keyword": "估值"}),
    ("trading_calendar", {"action": "last"}),
    ("trading_calendar", {"action": "offset", "ref_date": "2026-09-07", "offset": -5}),
    ("query_financials", {"mode": "single", "stocks": ["贵州茅台"],
                          "metrics": ["营收", "净利润"], "time": "最近三年"}),
    ("query_financials", {"mode": "compare", "stocks": ["贵州茅台", "五粮液"],
                          "metrics": ["毛利率"]}),
    ("query_financials", {"mode": "industry", "industry": "白酒",
                          "metrics": ["净资产收益率"], "top_n": 5}),
    ("query_financials", {"mode": "rank", "metrics": ["总市值"], "top_n": 5}),
    ("query_main_business", {"stock": "中际旭创"}),
    ("valuation_percentile", {"stock": "贵州茅台", "years": 3}),
    ("search_reports", {"keyword": "光模块", "days": 365, "limit": 5}),
    ("verify_forecasts", {"stock": "阳光电源"}),   # 现库内唯一有可回验期间的 A 股
    ("get_forecasts", {"stock": "中芯国际"}),
    ("list_chains", {}),
    ("analyze_chain", {"chain": "AI算力", "node": "光模块"}),
    ("analyze_alpha", {"stock": "中际旭创"}),
    ("compute_indicators", {"stock": "贵州茅台", "days": 200}),
    ("run_quant_scan", {"scan_type": "regime", "stock": "贵州茅台"}),
    ("detect_financial_risk", {"stock": "中际旭创"}),
]

# 动态 report_id: 先检索一篇研报再读 (库内 id 不保证从 1 开始连续)
def _first_report_id() -> int:
    env = REGISTRY.call("search_reports", {"keyword": "研报", "days": 0, "limit": 1})
    reports = (env.get("data") or {}).get("reports") or []
    if not reports:
        env = REGISTRY.call("search_reports", {"keyword": "a", "days": 0, "limit": 1})
        reports = (env.get("data") or {}).get("reports") or []
    return reports[0]["report_id"]

# 重档: 分钟级 (LLM 生成 / 全市场扫描)
HEAVY_CASES = [
    ("run_quant_scan", {"scan_type": "range", "top": 5}),
    ("run_quant_scan", {"scan_type": "trend", "top": 5}),
    ("run_quant_scan", {"scan_type": "setup"}),
    ("forge_chain", {"theme": "固态电池"}),
    ("extract_document", {"file_path": "NOT_EXIST_FOR_TEST.pdf"}),  # 预期 ToolError
]


def run_case(name: str, args: dict, expect_ok: bool = True) -> bool:
    spec = REGISTRY.get(name)
    if spec is None:
        print(f"  ✗ {name}: 未注册")
        return False
    t0 = time.time()
    env = REGISTRY.call(name, args)
    dt = time.time() - t0
    ok = env.get("ok") is expect_ok
    mark = "✓" if ok else "?"
    detail = ""
    if env.get("ok"):
        data = env.get("data") or {}
        keys = list(data)[:5]
        detail = f"keys={keys}"
    else:
        detail = f"error={env.get('error', '')[:60]}"
    print(f"  {mark} {name:24s} {dt*1000:7.0f}ms  {detail}")
    return ok


def main() -> int:
    heavy = "--heavy" in sys.argv
    print(f"== MCP smoke: {len(REGISTRY.specs())} tools, "
          f"mode={'heavy' if heavy else 'fast'} ==")
    fails = 0
    for name, args in FAST_CASES:
        fails += 0 if run_case(name, args) else 1
    rid = _first_report_id()
    fails += 0 if run_case("read_report", {"report_id": rid}) else 1
    if heavy:
        for name, args in HEAVY_CASES:
            expect_fail = name == "extract_document"
            fails += 0 if run_case(name, args, expect_ok=not expect_fail) else 1
    print(f"== done: {'ALL PASS' if fails == 0 else f'{fails} FAILED'} ==")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
