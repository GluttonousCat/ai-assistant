"""
评测用例生成器
生成 1000 条多样化 Text-to-SQL 查询用例, 覆盖:
- 财务单要素查询 (营收/净利/毛利率/ROE...)
- 行情查询 (股价/市值/PE/涨跌幅...)
- 时间修饰 (某年/最近N天/最近N年)
- 多股对比 / 单股多指标
- 边界与恶意用例 (无关输入/危险指令)

可复现: 固定 random seed
"""
from __future__ import annotations

import json
import random
import re
from typing import Dict, List

# ============================================================
# 素材库 (只含预期有把握的实体)
# ============================================================

# 财务有数据的股票 (income 存在) — 预期 "有结果"
FIN_STOCKS = [
    ("平安银行", "000001.SZ"),
    ("万科A", "000002.SZ"),
    ("深振业A", "000006.SZ"),
]

# 通用股票 (行情数据全量有, 财务未必) — 预期 "行情可查"
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

# 财务指标模板 -> (示例问题, 预期字段)
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

# 行情指标模板
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

# 年份模板
YEARS = ["2020年", "2021年", "2022年", "2023年", "2024年", "最近两年", "最近三年"]

# 动作词
VERBS = ["查询", "查一下", "看看", "帮我查", "请问", "查一查"]

# 问句后缀
SUFFIXES = ["", "是多少？", "怎么样？", "多少？", "的情况如何？"]

# 边界/恶意用例
EDGE_CASES = [
    ("我想吃火锅", "unknown"),
    ("今天天气怎么样", "unknown"),
    ("你好", "unknown"),
    ("分析复仇者联盟的财务数据", "query"),   # 无语义约束
    ("删除所有数据", "query"),                 # 危险意图(应被SQL校验拦截)
    ("DROP TABLE income", "query"),           # 危险SQL
    ("帮我写一首诗", "unknown"),
    ("QAQ", "unknown"),
]


def _rng():
    return random.Random(42)


def _pick_text(stock: str, metric: str, year: str = "") -> str:
    """组合一句查询文本"""
    verb = random.choice(VERBS)
    if year:
        return f"{verb}{stock}{year}的{metric}"
    return f"{verb}{stock}的{metric}"


def generate_cases(count: int = 1000) -> List[Dict]:
    """
    生成 count 条用例
    每条: {question, type, expected_intent, fin_stock(expected有财务数据)}
    type: financial / market / multi / edge
    """
    r = _rng()
    cases: List[Dict] = []

    # 分配比例 (approx): 40% 财务, 35% 行情, 10% 多股对比, 10% 多指标, 5% 边界
    n_fin = int(count * 0.40)
    n_mkt = int(count * 0.35)
    n_multi = int(count * 0.10)
    n_combined = int(count * 0.10)
    n_edge = count - n_fin - n_mkt - n_multi - n_combined

    # 1. 财务单要素
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
            "expected_has_result": True,   # 财务数据应在
            "expected_metric": metric,
        })

    # 2. 行情单要素
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

    # 3. 多股对比
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

    # 4. 单股多指标
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

    # 5. 边界用例
    for i, (q, expected) in enumerate(EDGE_CASES[:n_edge]):
        cases.append({
            "id": f"edge_{i:04d}",
            "question": q,
            "type": "edge",
            "expected_intent": expected,
            "fin_stock": False,
        })

    # 若边界不足, 补充
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
    # 分类统计
    from collections import Counter
    types = Counter(c["type"] for c in cases)
    print("类型分布:", dict(types))
    print()
    print("样本 10 条:")
    for c in cases[:10]:
        print(f"  [{c['type']}] {c['question']}")