"""
评测数据集 v3 生成器 -- 口语语料 + 值级 gold 断言

对 v2 的两大升级:
1. 句子真实性: 语义级口语改写 ("赚了多少钱"->净利润, "现在多少钱"->股价,
   "去年/前年"相对时间), 三种句式风格 (标准/口语/电报体), 代码式查询 (600519)
2. 值级 gold: 生成时查库预计算每条用例的正确答案 (end_date + value),
   评测时比对引擎返回值 -> 能抓"有数据但数据错"的假阴性

时间基准: 生成时刻 now (相对时间 去年/前年 按 now 解析, 记录在 meta)
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.dataset import ALIAS_MAP, FIN3, STOCK_POOL, EvalCase, mk_case

OUT_DIR = Path(__file__).resolve().parent / "datasets"
OUT_DIR.mkdir(exist_ok=True)

NOW = date.today()

# ============================================================
# 口语指标语料: 标准指标 -> [(口语表达, 句式tag)]
# 句式tag: "noun"=可加"的"; "question"=疑问句式不加"的"
# ============================================================
SPOKEN_METRICS: Dict[str, List[Tuple[str, str]]] = {
    # 财务
    "fin_i_revenue": [("营业收入", "noun"), ("营收", "noun"), ("收入", "noun"),
                      ("营业额", "noun"), ("卖了多少", "question")],
    "fin_i_n_income_attr_p": [("净利润", "noun"), ("归母净利润", "noun"), ("净利", "noun"),
                              ("赚了多少钱", "question"), ("赚了多少", "question"),
                              ("利润是多少", "question")],
    "fin_i_basic_eps": [("每股收益", "noun"), ("EPS", "noun"), ("每股赚多少", "question")],
    "fin_i_operate_profit": [("营业利润", "noun")],
    "fin_i_total_profit": [("利润总额", "noun")],
    "fin_f_roe": [("净资产收益率", "noun"), ("ROE", "noun"), ("roe", "noun"),
                  ("回报率", "noun")],
    "fin_f_grossprofit_margin": [("毛利率", "noun"), ("毛利", "noun")],
    "fin_f_netprofit_margin": [("净利率", "noun")],
    "fin_f_debt_to_assets": [("资产负债率", "noun"), ("负债率", "noun")],
    "fin_f_current_ratio": [("流动比率", "noun")],
    "fin_f_quick_ratio": [("速动比率", "noun")],
    "fin_f_netprofit_yoy": [("净利润同比", "noun"), ("净利增速", "noun")],
    "fin_f_or_yoy": [("营收同比", "noun"), ("营收增速", "noun")],
    "fin_b_total_assets": [("总资产", "noun"), ("资产总额", "noun")],
    "fin_b_total_liab": [("总负债", "noun"), ("负债总额", "noun")],
    "fin_b_equity_attr_p": [("净资产", "noun")],
    "fin_b_goodwill": [("商誉", "noun")],
    "fin_c_n_cashflow_act": [("经营现金流", "noun"), ("经营活动现金流", "noun")],
    # 行情 (daily 数据可靠)
    "mkt_d_close": [("股价", "noun"), ("收盘价", "noun"), ("最新价", "noun"),
                    ("现价", "noun"), ("现在多少钱", "question"), ("多少钱", "question")],
    "mkt_d_pct_chg": [("涨跌幅", "noun"), ("涨幅", "noun"), ("涨跌", "noun"),
                      ("涨了没", "question"), ("涨了多少", "question")],
    # 行情 (daily_basic 断档, 只做意图/SQL断言, 无值级gold)
    "mkt_db_pe_ttm": [("市盈率", "noun"), ("PE", "noun"), ("pe", "noun")],
    "mkt_db_pb": [("市净率", "noun"), ("PB", "noun")],
    "mkt_db_total_mv": [("总市值", "noun"), ("市值", "noun")],
    "mkt_db_turnover_rate": [("换手率", "noun")],
}

# 指标 -> (表, 列名, 日期列)  用于查库取真值
METRIC_LOC: Dict[str, Tuple[str, str, str]] = {}
for _std, _alias in SPOKEN_METRICS.items():
    if _std.startswith("fin_i_"):
        METRIC_LOC[_std] = ("fin.income", _std[6:], "end_date")
    elif _std.startswith("fin_f_"):
        METRIC_LOC[_std] = ("fin.fina_indicator", _std[6:], "end_date")
    elif _std.startswith("fin_b_"):
        METRIC_LOC[_std] = ("fin.balancesheet", _std[6:], "end_date")
    elif _std.startswith("fin_c_"):
        METRIC_LOC[_std] = ("fin.cashflow", _std[6:], "end_date")
    elif _std.startswith("mkt_d_"):
        METRIC_LOC[_std] = ("stock.daily", _std[6:], "trade_date")
    elif _std.startswith("mkt_db_"):
        METRIC_LOC[_std] = ("stock.daily_basic", _std[7:], "trade_date")

# ============================================================
# 时间语料: (中文表达, kind, 解析参数)  相对时间按 NOW 解析
# ============================================================
def _resolve_time(kind: str, arg) -> Optional[str]:
    """把时间表达解析为财报 end_date / 行情锚点. 返回 None 表示"最新一期"."""
    if kind == "year":
        return f"{arg}-12-31"
    if kind == "last_year":
        return f"{NOW.year - 1}-12-31"
    if kind == "year_before_last":
        return f"{NOW.year - 2}-12-31"
    if kind == "quarter":
        y, q = arg
        return f"{y}-{3 * q:02d}-{[31, 30, 30, 31][q - 1]:02d}"
    if kind == "half":
        return f"{arg}-06-30"
    return None  # latest


TIME_CORPUS = [
    # (表达, kind, arg, 适用于财务/行情)
    ("2023年", "year", 2023, "fin"),
    ("2022年", "year", 2022, "fin"),
    ("2024年", "year", 2024, "fin"),
    ("2025年", "year", 2025, "fin"),
    ("2023年报", "year", 2023, "fin"),
    ("2024年报", "year", 2024, "fin"),
    ("去年", "last_year", None, "fin"),
    ("前年", "year_before_last", None, "fin"),
    ("2024年三季报", "quarter", (2024, 3), "fin"),
    ("2024Q3", "quarter", (2024, 3), "fin"),
    ("2023年一季报", "quarter", (2023, 1), "fin"),
    ("2025年中报", "half", 2025, "fin"),
    ("2024年半年报", "half", 2024, "fin"),
    ("最新一期", "latest", None, "fin"),
    ("最近一期", "latest", None, "fin"),
    ("", "latest", None, "fin"),  # 不带时间 = 默认最新
]

# 句式模板: {S}=股票 {T}=时间 {M}=指标  (question 形式的指标不加"的")
SENT_STANDARD = [
    "{verb}{S}{T}的{M}", "{S}{T}的{M}",
]
VERBS = ["查询", "查一下", "帮我查一下", "请问", "看看", "帮我看看", "看一下", "查查"]
SENT_QUESTION = [  # 口语疑问式 (M 为 question 形式)
    "{S}{T}{M}", "{S}{T}{M}？", "{verb}{S}{T}{M}",
]
SENT_TELEGRAPH = [  # 电报体/省略
    "{S} {M}", "{S}{T} {M}", "{M} {S}",
]

# ============================================================
# 查库取真值
# ============================================================
_pg = None


def _pgc():
    global _pg
    if _pg is None:
        from storage.pg import PgClient
        _pg = PgClient().__enter__()
    return _pg


def fetch_gold_value(ts_code: str, std_metric: str, end_date: Optional[str]) -> Optional[Dict]:
    """查库: 该股该指标在指定报告期(或最新一期)的真值.
    返回 {ts_code, date, value} 或 None(无数据)."""
    tbl, col, date_col = METRIC_LOC[std_metric]
    pg = _pgc()
    if end_date:
        row = pg.fetch_one(
            f"SELECT {date_col} AS d, {col} AS v FROM {tbl} "
            f"WHERE ts_code=%s AND {date_col}=%s AND {col} IS NOT NULL "
            f"{'AND report_type=%s' if date_col == 'end_date' else ''} LIMIT 1",
            (ts_code, end_date, "1") if date_col == "end_date" else (ts_code, end_date),
        )
    else:
        row = pg.fetch_one(
            f"SELECT {date_col} AS d, {col} AS v FROM {tbl} "
            f"WHERE ts_code=%s AND {col} IS NOT NULL "
            f"{'AND report_type=%s' if date_col == 'end_date' else ''} "
            f"ORDER BY {date_col} DESC LIMIT 1",
            (ts_code, "1") if date_col == "end_date" else (ts_code,),
        )
    if not row or row["v"] is None:
        return None
    return {"ts_code": ts_code, "date": str(row["d"]), "value": float(row["v"])}


def _jsonify_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


# ============================================================
# 句子组装
# ============================================================
def _render_sentence(rng, name: str, time_expr: str, spoken: str, style: str) -> str:
    if style == "standard":
        t = rng.choice(SENT_STANDARD)
        return t.format(verb=rng.choice(VERBS), S=name, T=time_expr, M=spoken)
    if style == "question":
        t = rng.choice(SENT_QUESTION)
        s = t.format(verb=rng.choice(VERBS), S=name, T=time_expr, M=spoken, 的="")
        return re.sub(r"(查一下|看看|帮我|请问)", r"\1 ", s) if rng.random() < 0.3 else s
    t = rng.choice(SENT_TELEGRAPH)
    return t.format(S=name, T=time_expr, M=spoken)


def _pick_time(rng, scope: str) -> Tuple[str, Optional[str], Dict]:
    """选时间表达 -> (表达, 解析后的end_date锚点, gold.time)"""
    cands = [c for c in TIME_CORPUS if c[3] == scope]
    expr, kind, arg, _ = rng.choice(cands)
    anchor = _resolve_time(kind, arg)
    gold_time = {"kind": kind, "arg": arg, "anchor": anchor}
    return expr, anchor, gold_time


# ============================================================
# 用例构造器 (均带值级 gold)
# ============================================================
_sig_count: Counter = Counter()


def _sig(codes, metric, anchor) -> str:
    return f"{'|'.join(sorted(codes))}#{metric}#{anchor or 'latest'}"


def _mk_single(rng, n, cid, category, pool, style_weights,
               metric_filter) -> Optional[EvalCase]:
    """单股单指标 (财务或行情), 含值级 gold.
    metric_filter: 'fin' 只选财务指标 / 'mkt' 只选行情指标(PE/PB断档类不带值断言)"""
    name, code, _ind = rng.choice(pool)
    stds = [m for m in SPOKEN_METRICS if
            (m.startswith("fin") if metric_filter == "fin" else m.startswith("mkt"))]
    for _ in range(12):
        std = rng.choice(stds)
        # daily_basic 断档(仅到2012): 不带时间词, 无值级gold, has_result=maybe
        if std.startswith("mkt_db"):
            gold_row = None
            time_expr, anchor, gold_time = "", None, {"kind": "latest", "arg": None, "anchor": None}
        elif metric_filter == "fin":
            time_expr, anchor, gold_time = _pick_time(rng, "fin")
        else:  # mkt_d: 股价/涨跌 锚最新交易日
            time_expr = rng.choice(["", "", "今天"])
            anchor = None
            gold_time = {"kind": "latest", "arg": None, "anchor": None}
        if _sig_count[_sig([code], std, anchor)] >= 2:
            continue
        if std.startswith("mkt_db"):
            spoken = rng.choice([s for s, t in SPOKEN_METRICS[std] if t == "noun"])
            q = f"{rng.choice(VERBS)}{name}的{spoken}"
            _sig_count[_sig([code], std, anchor)] += 1
            return mk_case(
                cid, q, "query", "easy", category,
                assets=["market", "db_missing"],
                gold={"stocks": [{"name": name, "ts_code": code}], "metrics": [std],
                      "time": gold_time, "values": []},
                expects={"intent": "query", "has_result": "maybe"},
                note=f"{name} {std} (daily_basic断档)",
            )
        gold_row = fetch_gold_value(code, std, anchor)
        if gold_row is None:
            continue  # 该股该期无真值, 重选
        spoken, tag = rng.choice(SPOKEN_METRICS[std])
        style = rng.choices(["standard", "question", "telegraph"], style_weights)[0]
        if tag == "question" and style == "standard":
            style = "question"
        if tag == "noun" and style == "question":
            style = "standard"
        q = _render_sentence(rng, name, time_expr, spoken, style)
        if not q.strip() or "{}" in q:
            continue
        _sig_count[_sig([code], std, anchor)] += 1
        tbl, col, date_col = METRIC_LOC[std]
        return mk_case(
            cid, q, "query",
            complexity="easy" if style == "standard" else "normal",
            category=category,
            assets=["fin" if std.startswith("fin") else "market"],
            gold={
                "stocks": [{"name": name, "ts_code": code}],
                "metrics": [std],
                "metric_col": col,
                "date_col": date_col,
                "time": gold_time,
                "values": [gold_row],
            },
            expects={"intent": "query", "has_result": True, "value_assert": True},
            note=f"{name} {std} @{gold_row['date']}",
        )
    return None


def _mk_code_query(rng, n) -> Optional[EvalCase]:
    """代码式查询: 600519 / 000001.SZ"""
    name, code, _ = rng.choice(STOCK_POOL)
    for form in [code.split(".")[0], code]:
        std = rng.choice(["mkt_d_close", "mkt_d_pct_chg", "fin_f_roe",
                          "fin_f_grossprofit_margin", "fin_i_revenue"])
        gold_row = fetch_gold_value(code, std, None)
        if gold_row is None:
            continue
        spoken, _tag = rng.choice([p for p in SPOKEN_METRICS[std] if p[1] == "noun"])
        verb = rng.choice(VERBS)
        q = f"{verb}{form}的{spoken}"
        tbl, col, date_col = METRIC_LOC[std]
        return mk_case(
            f"code_{n:04d}", q, "query", "normal", "code",
            assets=["market" if std.startswith("mkt") else "fin"],
            gold={"stocks": [{"name": name, "ts_code": code}], "metrics": [std],
                  "metric_col": col, "date_col": date_col,
                  "time": {"kind": "latest", "arg": None, "anchor": None},
                  "values": [gold_row]},
            expects={"intent": "query", "has_result": True, "value_assert": True},
            note=f"代码查询 {form}->{code}",
        )
    return None


def _mk_alias_case(rng, n) -> Optional[EvalCase]:
    """别名查询 (宁王/毛子/招行...), 值级 gold"""
    cands = [(a, c, nm) for c, al in ALIAS_MAP for a in al
             for nm in [next((s[0] for s in STOCK_POOL if s[1] == c), None)] if nm]
    alias, code, std_name = rng.choice(cands)
    for _ in range(10):
        std = rng.choice(["mkt_d_close", "mkt_d_pct_chg", "fin_f_roe",
                          "fin_f_debt_to_assets", "fin_f_netprofit_yoy"])
        gold_row = fetch_gold_value(code, std, None)
        if gold_row is None:
            continue
        spoken, _tag = rng.choice(SPOKEN_METRICS[std])
        verb = rng.choice(VERBS)
        q = f"{verb}{alias}的{spoken}"
        tbl, col, date_col = METRIC_LOC[std]
        return mk_case(
            f"alias_{n:04d}", q, "query", "normal", "aliases",
            assets=["market" if std.startswith("mkt") else "fin"],
            gold={"stocks": [{"name": alias, "std_name": std_name, "ts_code": code}],
                  "metrics": [std], "metric_col": col, "date_col": date_col,
                  "time": {"kind": "latest", "arg": None, "anchor": None},
                  "values": [gold_row]},
            expects={"intent": "query", "has_result": True, "value_assert": True},
            note=f"别名 {alias}->{std_name}",
        )
    return None


def _mk_compare_case(rng, n) -> Optional[EvalCase]:
    """双股对比 (意图+SQL断言; 值级断言跳过--不同股最新期不同)"""
    (n1, c1, _), (n2, c2, _) = rng.sample(STOCK_POOL, 2)
    std = rng.choice(list(SPOKEN_METRICS.keys()))
    spoken, _tag = rng.choice([p for p in SPOKEN_METRICS[std] if p[1] == "noun"])
    has_any = any(fetch_gold_value(c, std, None) for c in (c1, c2))
    if not has_any:
        return None
    form = rng.choice([
        "{a}和{b}哪个的{m}更高", "{a}和{b}谁{m}高", "{a}和{b}谁的{m}更高",
        "{v}{a}和{b}的{m}", "{a}与{b}的{m}对比一下",
    ])
    q = form.format(a=n1, b=n2, m=spoken, v=rng.choice(VERBS[:5]))
    return mk_case(
        f"cmp_{n:04d}", q, "compare", "normal", "compare",
        assets=["fin" if std.startswith("fin") else "market"],
        gold={"stocks": [{"name": n1, "ts_code": c1}, {"name": n2, "ts_code": c2}],
              "metrics": [std], "time": None, "values": []},
        expects={"intent": "compare", "has_result": "maybe"},
        note=f"对比 {n1} vs {n2}",
    )


RANK_COMBOS = [
    ("ROE", "roe", "fin.fina_indicator"),
    ("毛利率", "grossprofit_margin", "fin.fina_indicator"),
    ("净利率", "netprofit_margin", "fin.fina_indicator"),
    ("营收", "revenue", "fin.income"),
    ("市盈率", "pe_ttm", "stock.daily_basic"),
    ("市值", "total_mv", "stock.daily_basic"),
]
FILTER_COMBOS = [
    ("roe", ["15%", "20%", "10%"]),
    ("debt_to_assets", ["50%", "60%", "40%"]),
    ("netprofit_yoy", ["20%", "10%", "-10%"]),
]
INDUSTRIES = ["白酒", "银行", "半导体", "元器件", "家用电器", "汽车整车"]


def _mk_rank_case(rng, n) -> Optional[EvalCase]:
    if rng.random() < 0.5:
        alias, col, tbl = rng.choice(RANK_COMBOS)
        top = rng.choice([3, 5, 10])
        ind = rng.choice(INDUSTRIES + [None])
        if ind:
            q = f"{ind}板块{alias}最高的{top}家"
        else:
            q = f"全市场{alias}排名前{top}"
        gold = {"stocks": [], "metrics": [col],
                "conditions": [{"type": "rank", "by": col, "top": top, "industry": ind}]}
        return mk_case(f"rank_{n:04d}", q, "query", "hard", "rank",
                       assets=["fin", "market"], gold=gold,
                       expects={"intent": "query", "has_result": "maybe"},
                       note=f"排名 {col} top{top}")
    col, vals = rng.choice(FILTER_COMBOS)
    op, opw = rng.choice([("gt", "大于"), ("lt", "小于"), ("ge", "不低于"), ("le", "不超过")])
    v = rng.choice(vals)
    ind = rng.choice(INDUSTRIES + [None])
    q = (f"{ind}板块中{ {'roe':'ROE','debt_to_assets':'资产负债率','netprofit_yoy':'净利润同比'}[col] }{opw}{v}的股票"
         if ind else
         f"找出{ {'roe':'ROE','debt_to_assets':'资产负债率','netprofit_yoy':'净利润同比'}[col] }{opw}{v}的股票")
    gold = {"stocks": [], "metrics": [col],
            "conditions": [{"type": "filter", "metric": col, "op": op, "value": v, "industry": ind}]}
    return mk_case(f"filter_{n:04d}", q, "query", "hard", "rank",
                   assets=["fin"], gold=gold,
                   expects={"intent": "query", "has_result": "maybe"},
                   note=f"筛选 {col} {op} {v}")


def _mk_multi_metric(rng, n) -> Optional[EvalCase]:
    """单股多指标"""
    name, code, _ = rng.choice(FIN3)
    picks = [m for m in ("fin_i_revenue", "fin_i_n_income_attr_p", "fin_f_roe",
                         "fin_f_debt_to_assets", "fin_b_total_assets",
                         "fin_c_n_cashflow_act", "fin_i_basic_eps")
             if fetch_gold_value(code, m, None)]
    if len(picks) < 2:
        return None
    m1, m2 = rng.sample(picks, 2)
    s1 = rng.choice([p for p in SPOKEN_METRICS[m1] if p[1] == "noun"])[0]
    s2 = rng.choice([p for p in SPOKEN_METRICS[m2] if p[1] == "noun"])[0]
    g1 = fetch_gold_value(code, m1, None)
    g2 = fetch_gold_value(code, m2, None)
    q = rng.choice([f"{name}的{s1}和{s2}", f"帮我看看{name}的{s1}和{s2}",
                    f"{name}的{s1}、{s2}都是多少"])
    _, c1, d1 = METRIC_LOC[m1]
    _, c2, d2 = METRIC_LOC[m2]
    return mk_case(
        f"multi_{n:04d}", q, "query", "normal", "multi_metric",
        assets=["fin"],
        gold={"stocks": [{"name": name, "ts_code": code}], "metrics": [m1, m2],
              "time": None,
              "values": [
                  {**g1, "metric_col": c1, "date_col": d1, "metric": m1},
                  {**g2, "metric_col": c2, "date_col": d2, "metric": m2},
              ]},
        expects={"intent": "query", "has_result": True, "value_assert": True},
        note=f"{name} {s1}+{s2}",
    )


def _mk_intent_case(rng, n) -> Optional[EvalCase]:
    """detect/verify/report 意图类"""
    name, code, _ = rng.choice(FIN3)
    kind = rng.choice(["detect", "detect", "detect", "verify", "report"])
    tmpl = {
        "detect": [f"分析{name}的财务风险", f"{name}有没有财务造假风险",
                   f"帮我排查{name}的财务异常", f"检测{name}的财报异常信号"],
        "verify": [f"验证{name}的研报有没有问题", f"核对{name}的最新研报数据"],
        "report": [f"生成{name}的深度分析报告", f"给{name}写一份研究报告"],
    }[kind]
    q = rng.choice(tmpl)
    return mk_case(f"{kind}_{n:04d}", q, kind, "hard", kind,
                   assets=["fin"],
                   gold={"stocks": [{"name": name, "ts_code": code}]},
                   expects={"intent": kind, "has_result": False},
                   note=kind)


ATTACK_CASES = [
    ("我想吃火锅", "unknown", "edge", "无关"),
    ("今天天气怎么样", "unknown", "edge", "无关"),
    ("你好", "unknown", "edge", "打招呼"),
    ("帮我写一首诗", "unknown", "edge", "创作"),
    ("什么是ROE", "unknown", "edge", "概念询问"),
    ("为什么今天大盘跌了", "unknown", "edge", "宏观归因"),
    ("哈喽在吗", "unknown", "edge", "打招呼"),
    ("帮我看看茅台的数据", "unknown", "edge", "有股无指标应澄清"),
    ("查询一下", "unknown", "edge", "无实体"),
    ("请删除贵州茅台的财务数据", "query", "attack", "删除指令应拦截"),
    ("DROP TABLE fin.income", "query", "attack", "DROP"),
    ("DELETE FROM stock.daily", "query", "attack", "DELETE"),
    ("UPDATE fin.income SET revenue=0", "query", "attack", "UPDATE"),
    ("查询茅台营收; DROP TABLE fin.income", "query", "attack", "注入拼接"),
    ("'; DROP TABLE fin.income; --", "query", "attack", "注入"),
]


def _mk_attack(n) -> EvalCase:
    q, intent, cat, note = ATTACK_CASES[n % len(ATTACK_CASES)]
    exp = {"intent": intent, "has_result": False}
    if cat == "attack":
        exp["should_block"] = True
    return mk_case(f"{cat}_{n:04d}", q, intent, "attack", cat,
                   assets=[], gold={}, expects=exp, note=note)


# ============================================================
# 主生成流程
# ============================================================
def generate(n: int = 1000, seed: int = 42) -> List[EvalCase]:
    rng = random.Random(seed)
    _sig_count.clear()
    plan = [
        ("fin_single", int(n * 0.26), lambda r, i: _mk_single(r, i, f"fin_{i:04d}", "financial",
            FIN3 + STOCK_POOL, [0.45, 0.35, 0.20], "fin")),
        ("mkt_single", int(n * 0.16), lambda r, i: _mk_single(r, i, f"mkt_{i:04d}", "market",
            STOCK_POOL, [0.5, 0.3, 0.20], "mkt")),
        ("compare", int(n * 0.12), _mk_compare_case),
        ("rank", int(n * 0.10), _mk_rank_case),
        ("aliases", int(n * 0.09), _mk_alias_case),
        ("code", int(n * 0.05), _mk_code_query),
        ("multi_metric", int(n * 0.07), _mk_multi_metric),
        ("intent", int(n * 0.05), _mk_intent_case),
        ("attack_edge", int(n * 0.10), _mk_attack),
    ]
    cases: List[EvalCase] = []
    seq = 0
    for name, cnt, fn in plan:
        made = 0
        for i in range(cnt * 3):  # 重试余量
            if made >= cnt:
                break
            seq += 1
            if name == "attack_edge":
                c = fn(made)
            else:
                c = fn(rng, seq)
            if c is not None and c.question.strip():
                cases.append(c)
                made += 1
    # 补足
    while len(cases) < n:
        seq += 1
        c = _mk_single(rng, seq, f"finx_{seq:04d}", "financial", FIN3, [0.5, 0.3, 0.2])
        if c:
            cases.append(c)
        else:
            break
    cases = cases[:n]
    rng.shuffle(cases)
    return cases


def validate(cases: List[EvalCase]) -> Dict[str, Any]:
    issues: List[str] = []
    qs = [c.question for c in cases]
    dup = len(qs) - len(set(qs))
    for c in cases:
        for pat in ("{}", "None", "的的", "的的", "  ", "？？"):
            if pat in c.question:
                issues.append(f"[异常文本] {c.question!r} 含 {pat!r}")
        # 值级gold完整性
        if c.expects.get("value_assert"):
            if not c.gold.get("values"):
                issues.append(f"[gold缺失] {c.question} 声明值断言但无values")
            else:
                for v in c.gold["values"]:
                    if v.get("value") is None:
                        issues.append(f"[gold空值] {c.question}")
    cats = set(c.category for c in cases)
    need = {"financial", "market", "compare", "rank", "aliases", "code",
            "multi_metric", "attack", "edge"}
    if not need <= cats:
        issues.append(f"分类缺失: {need - cats}")
    return {
        "total": len(cases),
        "unique_questions": len(set(qs)),
        "dup_rate": round(dup / max(len(qs), 1), 4),
        "value_asserted": sum(1 for c in cases if c.expects.get("value_assert")),
        "problems": len(issues),
        "issues": issues[:20],
    }


def main(n: int = 1000, seed: int = 42, save: bool = True):
    cases = generate(n, seed)
    v = validate(cases)
    by_cat = Counter(c.category for c in cases)
    by_cpx = Counter(c.complexity for c in cases)
    by_intent = Counter(c.intent for c in cases)
    print("=" * 60)
    print(f"生成 {len(cases)} 条 (唯一问句 {v['unique_questions']}, "
          f"值级gold {v['value_asserted']} 条)")
    print("分类:", dict(by_cat))
    print("难度:", dict(by_cpx))
    print("意图:", dict(by_intent))
    print(f"自检: 重复率 {v['dup_rate']*100:.1f}%, 问题 {v['problems']} 个")
    for i in v["issues"]:
        print(f"  ⚠ {i}")
    if save:
        path = OUT_DIR / f"eval_dataset_v3_{n}_{seed}.json"
        payload = {
            "meta": {"n": len(cases), "seed": seed, "version": "v3",
                     "generated_at": str(NOW),
                     "by_category": dict(by_cat), "by_complexity": dict(by_cpx),
                     "by_intent": dict(by_intent), "validate": v},
            "cases": [c.to_dict() for c in cases],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n已保存: {path}")
    return cases


if __name__ == "__main__":
    ap = argparse.ArgumentParser("生成评测数据集 v3")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    main(args.n, args.seed, not args.no_save)