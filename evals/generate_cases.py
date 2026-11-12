"""
Eval case generator
Generates 1000 diverse Text-to-SQL query cases covering:
- Single-factor financial queries (revenue/net profit/gross margin/ROE...)
- Quote queries (price/market cap/PE/price change...)
- Time modifiers (a given year/last N days/last N years)
- Multi-stock comparison / single stock with multiple metrics
- Boundary and malicious cases (irrelevant input/dangerous commands)

Reproducible: fixed random seed
"""
from __future__ import annotations

import random
from typing import Dict, List

# ============================================================
# Corpus (only entities with confident expectations)
# ============================================================

# Stocks with financial data (income exists) — expected "has result"
FIN_STOCKS = [
    ("平安银行", "000001.SZ"),
    ("万科A", "000002.SZ"),
    ("深振业A", "000006.SZ"),
]

# General stocks (quotes fully available, financials maybe) — expected "quotes queryable"
MARKET_STOCKS = [
    ("平安银行", "000001.SZ"),
    ("万科A", "000002.SZ"),
    ("深振业A", "000006.SZ"),
    ("宁德时代", "300750.SZ"),
    ("贵州茅台", "600519.SH"),
    ("招商银行", "600036.SH"),
    ("中国平安", "601318.SH"),
    ("比亚迪", "002594.SZ"),
    ("五粮液", "000858.SZ"),
    ("格力电器", "000651.SZ"),
]

# Financial metric templates -> (sample question, expected field)
FIN_METRICS = [
    ("营收", "营收"),
    ("营业收入", "营业收入"),
    ("净利润", "净利润"),
    ("归母净利润", "归母净利润"),
    ("营业利润", "营业利润"),
    ("毛利率", "毛利率"),
    ("净利率", "净利率"),
    ("ROE", "ROE"),
    ("净资产收益率", "净资产收益率"),
    ("资产负债率", "资产负债率"),
    ("流动比率", "流动比率"),
    ("每股收益", "每股收益"),
]

# Quote metric templates
MARKET_METRICS = [
    ("股价", "股价"),
    ("收盘价", "收盘价"),
    ("市盈率", "市盈率"),
    ("PE", "PE"),
    ("市净率", "市净率"),
    ("市值", "市值"),
    ("总市值", "总市值"),
    ("涨跌幅", "涨跌幅"),
    ("换手率", "换手率"),
]

# Year templates
YEARS = ["2020年", "2021年", "2022年", "2023年", "2024年", "最近两年", "最近三年"]

# Action verbs
VERBS = ["查询", "查一下", "看看", "帮我查", "请问", "查一查"]

# Question suffixes
SUFFIXES = ["", "是多少？", "怎么样？", "多少？", "的情况如何？"]

# Boundary/malicious cases
EDGE_CASES = [
    ("我想吃火锅", "unknown"),
    ("今天天气怎么样", "unknown"),
    ("你好", "unknown"),
    ("分析复仇者联盟的财务数据", "query"),   # no semantic constraint
    ("删除所有数据", "query"),                 # dangerous intent (should be blocked by SQL validation)
    ("DROP TABLE income", "query"),           # dangerous SQL
    ("帮我写一首诗", "unknown"),
    ("QAQ", "unknown"),
]


def _rng():
    return random.Random(42)


def _pick_text(stock: str, metric: str, year: str = "") -> str:
    """Assemble one query sentence"""
    verb = random.choice(VERBS)
    if year:
        return f"{verb}{stock}{year}的{metric}"
    return f"{verb}{stock}的{metric}"


def generate_cases(count: int = 1000) -> List[Dict]:
    """
    Generate count cases
    Each: {question, type, expected_intent, fin_stock (expected to have financial data)}
    type: financial / market / multi / edge
    """
    r = _rng()
    cases: List[Dict] = []

    # Mix (approx): 40% financial, 35% quotes, 10% multi-stock compare, 10% multi-metric, 5% edge
    n_fin = int(count * 0.40)
    n_mkt = int(count * 0.35)
    n_multi = int(count * 0.10)
    n_combined = int(count * 0.10)
    n_edge = count - n_fin - n_mkt - n_multi - n_combined

    # 1. Single-factor financial
    for i in range(n_fin):
        stock, code = r.choice(FIN_STOCKS)
        metric, _ = r.choice(FIN_METRICS)
        year = r.choice(YEARS) if r.random() < 0.7 else ""
        q = _pick_text(stock, metric, year)
        cases.append({
            "id": f"fin_{i:04d}",
            "question": q,
            "type": "financial",
            "expected_intent": "query",
            "fin_stock": True,
            "expected_has_result": True,   # financial data should exist
            "expected_metric": metric,
        })

    # 2. Single-factor quotes
    for i in range(n_mkt):
        stock, code = r.choice(MARKET_STOCKS)
        metric, _ = r.choice(MARKET_METRICS)
        q = _pick_text(stock, metric)
        cases.append({
            "id": f"mkt_{i:04d}",
            "question": q,
            "type": "market",
            "expected_intent": "query",
            "fin_stock": stock in [s for s, c in FIN_STOCKS],
            "expected_has_result": True,
            "expected_metric": metric,
        })

    # 3. Multi-stock comparison
    for i in range(n_multi):
        s1, _ = r.choice(MARKET_STOCKS)
        s2, _ = r.choice(MARKET_STOCKS)
        while s2 == s1:
            s2, _ = r.choice(MARKET_STOCKS)
        metric, _ = r.choice(FIN_METRICS + MARKET_METRICS)
        verb = r.choice(["对比", "比较", "哪个的", "谁更高"])
        q = f"{verb}{s1}和{s2}的{metric}"
        cases.append({
            "id": f"multi_{i:04d}",
            "question": q,
            "type": "multi_compare",
            "expected_intent": "compare",
            "fin_stock": False,
        })

    # 4. Single stock, multiple metrics
    for i in range(n_combined):
        stock, _ = r.choice(FIN_STOCKS)
        m1, _ = r.choice(FIN_METRICS)
        m2, _ = r.choice(FIN_METRICS)
        while m2 == m1:
            m2, _ = r.choice(FIN_METRICS)
        verb = r.choice(VERBS)
        q = f"{verb}{stock}的{m1}和{m2}"
        cases.append({
            "id": f"comb_{i:04d}",
            "question": q,
            "type": "combined",
            "expected_intent": "query",
            "fin_stock": True,
            "expected_has_result": True,
        })

    # 5. Edge cases
    for i, (q, expected) in enumerate(EDGE_CASES[:n_edge]):
        cases.append({
            "id": f"edge_{i:04d}",
            "question": q,
            "type": "edge",
            "expected_intent": expected,
            "fin_stock": False,
        })

    # If edge cases run short, top up
    while len(cases) < count:
        stock, code = r.choice(FIN_STOCKS)
        metric, _ = r.choice(FIN_METRICS)
        q = _pick_text(stock, metric)
        cases.append({
            "id": f"extra_{len(cases):04d}",
            "question": q,
            "type": "financial",
            "expected_intent": "query",
            "fin_stock": True,
            "expected_has_result": True,
            "expected_metric": metric,
        })

    return cases[:count]


if __name__ == "__main__":
    cases = generate_cases(1000)
    print(f"生成 {len(cases)} 条用例")
    # Type distribution stats
    from collections import Counter
    types = Counter(c["type"] for c in cases)
    print("类型分布:", dict(types))
    print()
    print("样本 10 条:")
    for c in cases[:10]:
        print(f"  [{c['type']}] {c['question']}")