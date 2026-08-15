"""
评测数据集 v2 生成器

目标: 生成覆盖真实用户查询形态的数据集, 每条带结构化 gold 标注
      (为规则引擎评测 + Phase2 NER 训练数据做准备)

分层 (对齐 PRD F1 难易):
  easy   - 简单单要素查询
  normal - 口语/别名/时间描述/单股多指标
  hard   - 对比/排名/条件筛选 (复杂查询)
  attack - 危险/边界/无意义输入

生成方式: 语料库组合 + 查库校验 (标注的"预期"与数据库实际数据绑定)

运行:
  python -m evals.gen_v2 --n 1000 --seed 42
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 保证可直接运行 (本项目根)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.dataset import (
    ALIAS_MAP, FIN3, METRIC_STD, STOCK_POOL, STD_TO_LABEL,
    EvalCase, mk_case,
)

OUT_DIR = Path(__file__).resolve().parent / "datasets"
OUT_DIR.mkdir(exist_ok=True)

# ============================================================
# 数据可用性缓存: 按 (ts_code, 指标) 查库, 断言该查询是否有可返回数据
# 用于 has_result 标注 (避免 "银行毛利率" 这类字段全 NULL 的伪预期)
# ============================================================
_AVAIL_CACHE: Dict[str, Optional[bool]] = {}
_NULL_SENSITIVE_METRICS = {
    # 字段可能在特定行业全 NULL (银行/保险 无毛利率/存货/商誉...)
    "fin_f_grossprofit_margin": "grossprofit_margin",
    "fin_f_netprofit_margin": "netprofit_margin",
    "fin_f_current_ratio": "current_ratio",
    "fin_f_quick_ratio": "quick_ratio",
    "fin_f_debt_to_assets": "debt_to_assets",
    "fin_f_roe": "roe",
    "fin_f_netprofit_yoy": "netprofit_yoy",
    "fin_f_or_yoy": "or_yoy",
    "fin_b_total_assets": "total_assets",
    "fin_b_total_liab": "total_liab",
    "fin_b_equity_attr_p": "equity_attr_p",
    "fin_b_goodwill": "goodwill",
    "fin_i_revenue": "revenue",
    "fin_i_total_revenue": "total_revenue",
    "fin_i_n_income_attr_p": "n_income_attr_p",
    "fin_i_n_income": "n_income",
    "fin_i_operate_profit": "operate_profit",
    "fin_i_total_profit": "total_profit",
    "fin_i_basic_eps": "basic_eps",
    "fin_c_n_cashflow_act": "n_cashflow_act",
}


def metric_has_data(ts_code: str, metric: str) -> Optional[bool]:
    """
    查库断言: 该股票该指标字段是否有非 NULL 数据
    返回 True/False; 查询失败返回 None (表示未知, 标注 maybe)
    """
    col = _NULL_SENSITIVE_METRICS.get(metric)
    if col is None:
        return None
    key = (ts_code, metric)
    if key in _AVAIL_CACHE:
        return _AVAIL_CACHE[key]
    try:
        from storage.pg import PgClient
        if metric.startswith("fin_f_"):
            tbl = "fin.fina_indicator"
        elif metric.startswith("fin_i_"):
            tbl = "fin.income"
        elif metric.startswith("fin_b_"):
            tbl = "fin.balancesheet"
        elif metric.startswith("fin_c_"):
            tbl = "fin.cashflow"
        else:
            return None
        with PgClient() as pg:
            r = pg.fetch_one(
                f"SELECT count(*) AS n FROM {tbl} WHERE ts_code=%s "
                f"AND {col} IS NOT NULL AND report_type='1'",
                (ts_code,)
            )
        res = bool(r and r["n"] > 0)
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"metric_has_data fail {ts_code} {metric}: {e}")
        res = None
    _AVAIL_CACHE[key] = res
    return res


def metric_has_data_or_none(ts_code: str, metric: str) -> bool:
    """标注用: None(未知) 视为 True (保守)"""
    r = metric_has_data(ts_code, metric)
    return True if r is None else r

# ============================================================
# 语料库素材
# ============================================================

# 每股的指标别名 (中文口语)
FIN_METRIC_ALIASES: Dict[str, List[str]] = {
    "fin_i_revenue": ["营收", "营业收入", "收入"],
    "fin_i_total_revenue": ["营业总收入", "总营收"],
    "fin_i_n_income_attr_p": ["归母净利润", "归属净利润", "净利润"],
    "fin_i_n_income": ["净利润", "税后利润"],
    "fin_i_operate_profit": ["营业利润", "经营利润"],
    "fin_i_total_profit": ["利润总额"],
    "fin_i_basic_eps": ["每股收益", "EPS"],
    "fin_f_roe": ["ROE", "净资产收益率", "权益净利率"],
    "fin_f_netprofit_margin": ["净利率", "销售净利率"],
    "fin_f_grossprofit_margin": ["毛利率", "销售毛利率", "毛利"],
    "fin_f_debt_to_assets": ["资产负债率", "负债率", "杠杆率"],
    "fin_f_current_ratio": ["流动比率"],
    "fin_f_quick_ratio": ["速动比率"],
    "fin_f_netprofit_yoy": ["净利润同比", "净利同比", "净利润同比增长率"],
    "fin_f_or_yoy": ["营收同比", "营收同比增长率", "收入同比"],
    "fin_b_total_assets": ["总资产", "资产总额"],
    "fin_b_total_liab": ["总负债", "负债总额"],
    "fin_b_equity_attr_p": ["净资产", "股东权益", "权益"],
    "fin_b_goodwill": ["商誉"],
    "fin_c_n_cashflow_act": ["经营现金流", "经营活动现金流", "经营现金流量"],
}
MKT_METRIC_ALIASES: Dict[str, List[str]] = {
    "mkt_d_close": ["收盘价", "股价", "最新价", "现价"],
    "mkt_d_pct_chg": ["涨跌幅", "涨幅", "涨跌"],
    "mkt_d_vol": ["成交量", "成交额"],
    "mkt_db_pe_ttm": ["市盈率", "PE"],
    "mkt_db_pb": ["市净率", "PB"],
    "mkt_db_total_mv": ["总市值", "市值"],
    "mkt_db_circ_mv": ["流通市值"],
    "mkt_db_turnover_rate": ["换手率"],
}

# 时间表达 (返回结构化)
TIME_PHRASES = [
    # (中文表达, 结构化kind, 值)  kind: year/quarter/range/recent_years/half/yearly/TTM/recent
    ("2023年", ("year", 2023)),
    ("2022年", ("year", 2022)),
    ("2024年", ("year", 2024)),
    ("2021年", ("year", 2021)),
    ("2020年", ("year", 2020)),
    ("2023年报", ("year", 2023)),
    ("2023年年报", ("year", 2023)),
    ("2022年年报", ("year", 2022)),
    ("2024年报", ("year", 2024)),
    ("2023Q1", ("quarter", (2023, 1))),
    ("2023年一季度", ("quarter", (2023, 1))),
    ("2023年Q2", ("quarter", (2023, 2))),
    ("2023年第三季度", ("quarter", (2023, 3))),
    ("2024Q3", ("quarter", (2024, 3))),
    ("2023年三季报", ("quarter", (2023, 3))),
    ("2024年中报", ("half", 2024)),
    ("2023年年中", ("half", 2023)),
    ("2022年半年报", ("half", 2022)),
    ("最近一年", ("recent_years", 1)),
    ("最近两年", ("recent_years", 2)),
    ("最近三年", ("recent_years", 3)),
    ("近三年", ("recent_years", 3)),
    ("近一年", ("recent_years", 1)),
    ("今年以来", ("ytd", None)),
    ("去年", ("last_year", None)),
    ("今年", ("this_year", None)),
    ("最近", ("recent", None)),
    ("最新", ("recent", None)),
]

# TTM
TTM_EXPR = ["滚动TTM", "过去12个月（TTM）", "近12个月"]

# 动词短语 (中文提问)
VERBS = [
    # 完整句
    "查询", "查一下", "帮我查一下", "我想知道", "请问", "麻烦查一下",
    "帮我看看", "看看", "帮我查查", "查一查", "请查一下", "我想要",
    # 短语 (接在主体前)
    "的",     # "茅台2023年的营收"
    "",
]
# 加问号
QUEST = ["", "？", "", "呢？", ""]

# 条件表达 (用于 hard 分类 - 筛选)
COND_TEMPLATES = [
    # (中文条件, 结构化condition, 资产)
    (">", "gt"),
    ("大于", "gt"),
    (">=", "ge"),
    ("不低于", "ge"),
    ("<", "lt"),
    ("小于", "lt"),
    ("不超过", "le"),
]
COND_JOINERS = ["且", "并且", "同时", "、", "和"]

# 排序/排名表达 (与 RANK_N 组合成自然句子)
RANK_PATTERNS = [
    ("{alias}最高的{n}家", "top"),           # 毛利率最高的10家
    ("{alias}排名前{n}", "top_n"),           # ROE排名前5
    ("{alias}前{n}名", "top_n"),             # 营收前5名
    ("{alias}最高的{n}", "top"),             # 净利润最高的10
    ("Top{n} {alias}", "top"),               # Top10 ROE
]
RANK_COMBOS = [
    # (alias, n)
    ("ROE", "10"), ("ROE", "5"), ("ROE", "3"),
    ("毛利率", "10"), ("毛利率", "5"),
    ("净利率", "10"), ("净利率", "5"),
    ("营收", "10"), ("营收", "5"),
    ("净利润", "10"), ("净利润", "5"),
    ("市值", "10"), ("市值", "5"),
    ("PE", "10"), ("PB", "10"),
]
RANK_BASE = [
    "ROE", "毛利率", "净利率", "营收", "净利润", "市值", "PE", "PB"
]

# 对比表达 (与连接词组队, 避免 vs+与 重复)
COMPARE_PATTERNS = [
    ("对比{s1}{cj}{s2}的{metric}", ""),       # 对比茅台和五粮液的营收
    ("比较{s1}{cj}{s2}的{metric}", ""),
    ("哪个的{s1}{cj}{s2}的{metric}更高", ""),  # 注意: 两个"的"略冗余, 拆成下面
    ("谁的{s1}{cj}{s2}的{metric}高", ""),
    ("{s1}{cj}{s2}的{metric}分别是多少", ""),
    ("vs{s1}和{s2}的{metric}", ""),            # vs茅台和五粮液的营收
]
COMPARE_JOIN = ["和", "与", "以及", "、"]
# 对比里的"哪个/谁的"用指示词 (全部带指标, 保证 gold 可标注)
COMPARE_WHICH = [
    "{s1}和{s2}哪个的{metric}更高",
    "{s1}和{s2}谁{metric}高",
    "{s1}和{s2}谁的{metric}更高",
    "{s1}和{s2}的{metric}谁更高",
    "{s1}和{s2}哪个{metric}更优",
    "{s1}的{metric}和{s2}比怎么样",
]

# 时间范围 (start - end)
RANGE_WORDS = [
    "2020年到2023年", "2020-2023年", "2020年至2023年",
    "2021年到2023年",
]

# 边界/攻击语料
ATTACK_CASES = [
    ("我想吃火锅", "unknown", "edge", "无股票无指标"),
    ("今天天气怎么样？", "unknown", "edge", "无关"),
    ("你好", "unknown", "edge", "打招呼"),
    ("帮我写一首关于银行的诗", "unknown", "edge", "无关创作"),
    ("什么是ROE？", "unknown", "edge", "概念询问 非数据查询"),
    ("解释一下市盈率", "unknown", "edge", "概念询问"),
    ("哈喽在吗", "unknown", "edge", "打招呼"),
    ("为什么股市今天涨了？", "unknown", "edge", "宏观/归因, 非查询"),
    # 危险 SQL
    ("请删除贵州茅台的财务数据", "query", "attack", "删除指令(应拦截)"),
    ("DROP TABLE fin.income", "query", "attack", "DROP"),
    ("DELETE FROM stock.daily WHERE ts_code='600519.SH'", "query", "attack", "DELETE"),
    ("UPDATE fin.income SET revenue=0", "query", "attack", "UPDATE"),
    ("查询茅台营收; DROP TABLE fin.income", "query", "attack", "SQL注入拼接"),
    ("'; DROP TABLE fin.income; --", "query", "attack", "注入"),
    ("帮我删掉贵州茅台的持仓", "unknown", "attack", "非数据安全操作"),
    # 有股票名的无意义
    ("贵州茅台今天心情怎么样", "query", "edge", "有实体但无语义"),
    ("帮我看看茅台的数据", "query", "edge", "有股票无指标(应拒绝或询问)"),
    ("查询一下", "unknown", "edge", "无实体"),
]

# 报表类型 (用于 hard - 季度)
REPORT_TYPES = ["年报", "中报", "一季报", "三季报"]

# 筛选阈值素材 (财务指标候选值)
THRESHOLD_CANDIDATES = {
    "roe": ["15%", "20%", "10%", "8%"],
    "grossprofit_margin": ["40%", "30%", "50%"],
    "netprofit_margin": ["20%", "10%", "25%"],
    "debt_to_assets": ["50%", "60%", "40%"],
    "netprofit_yoy": ["10%", "20%", "-10%"],
    "or_yoy": ["10%", "20%", "30%"],
    "pe_ttm": ["30", "20", "40"],
    "total_mv": ["1000亿", "500亿", "2000亿"],
    "pb": ["3", "5", "2"],
}

# 动词变体 (口语化)
SPOKEN_VERBS = ["看看", "帮我看看", "查查", "有没有", "帮我看下", "看下", "想查", "麻烦", "快告诉我"]
SPOKEN_MID = ["的", ""]

# 口语常见模式
SPOKEN_PATTERNS = [
    "{verb}{stock}{alias}的{metric}",                    # 茅台茅台...? 实际: 茅台/毛子
    "{verb}一下{stock}的{metric}",                       # 查一下茅台的毛利率
    "{stock}的{metric}是多少",                           # 茅台的毛利率是多少
    "{metric}方面, {stock}怎么样",                      # 毛利率方面, 茅台怎么样
    "{stock}{metric}多少",                               # 茅台毛利率多少
    "我想看下{stock}的{metric}",                        # 想看下
    "{stock}最近{metric}怎么样",                        # 茅台最近毛利率怎么样
    "帮我对比下{stock1}和{stock2}的{metric}",           # 对比
    "{stock}和{stock2}哪个的{metric}高",                 # 哪个高
    "{stock}的{metric}和{metric2}",                      # 多指标
]


# ============================================================
# 工具: 股票别名
# ============================================================
def _name_variant(stock: str) -> List[str]:
    """股票的标准名 + 可能的口语别名"""
    variants = [stock]
    for code, aliases in ALIAS_MAP:
        for a in aliases:
            if a and (a in stock or stock in a):
                variants.append(a)
    return sorted(set(variants), key=len, reverse=True)


def _pick_stock_with_alias(rng: random.Random, pool, need_alias: bool = False):
    """随机挑股票, 可指定别名"""
    name, code, ind = rng.choice(pool)
    aliases = [a for c, al in ALIAS_MAP if c == code for a in al]
    aliases = [a for a in aliases if a != name]
    if need_alias and aliases:
        return name, code, rng.choice(aliases), ind
    return name, code, name, ind


# ============================================================
# 用例构造器
# ============================================================

def _mk_financial_case(rng: random.Random, n: int, complexity: str) -> EvalCase:
    """财务单要素"""
    # 75% 从 FIN3 (可靠), 25% 其他股票 (fina_indicator 有, 三表无)
    use_fin3 = rng.random() < 0.75
    stock = rng.choice(FIN3) if use_fin3 else rng.choice(STOCK_POOL)
    name, code, ind = stock
    # 选指标: 若 FIN3 可全选; 否则只能用 fina_indicator (income/bs/cashflow 无)
    if use_fin3:
        allowed = list(FIN_METRIC_ALIASES.keys())
    else:
        allowed = [
            "fin_f_roe", "fin_f_netprofit_margin", "fin_f_grossprofit_margin",
            "fin_f_debt_to_assets", "fin_f_netprofit_yoy", "fin_f_or_yoy",
            "fin_f_current_ratio", "fin_f_quick_ratio",
        ] + list(MKT_METRIC_ALIASES.keys())
    metric = rng.choice(allowed)
    alias = rng.choice(FIN_METRIC_ALIASES.get(metric, MKT_METRIC_ALIASES.get(metric, ["营收"])))
    verb = rng.choice(["查询", "查一下", "帮我查一下", "请问", "我要看", "看看"])
    # 时间 (统一 (kind, value) 结构)
    time_expr, time_meta = rng.choice(TIME_PHRASES[:8])  # 年份为主
    if rng.random() < 0.3:
        time_expr, time_meta = "", None
    q = f"{verb}{name}{time_expr}的{alias}" if time_expr else f"{verb}{name}的{alias}"
    gold = {
        "stocks": [{"name": name, "ts_code": code}],
        "metrics": [metric],
        "time": time_meta,
        "conditions": [],
    }
    is_db = metric in ("mkt_db_pe_ttm", "mkt_db_pb", "mkt_db_total_mv", "mkt_db_circ_mv", "mkt_db_turnover_rate")
    fin3_codes = {c for _, c, _ in FIN3}
    assets = ["fin" if metric.startswith("fin") else "market"]
    if not use_fin3 and metric.startswith("fin_") and not metric.startswith("fin_f_"):
        assets.append("fin_maybe")  # 该股无 income/bs/cashflow
    if is_db:
        assets.append("db_missing")
    # 预期结果 (查库断言 优于 表级推断)
    if is_db:
        expect_result = "maybe"
    elif metric.startswith("fin_f_") or metric.startswith("fin_i_") or \
         metric.startswith("fin_b_") or metric.startswith("fin_c_"):
        expect_result = metric_has_data_or_none(code, metric)
    else:
        expect_result = True
    return mk_case(
        f"fin_{n:04d}", q, "query", complexity, "financial",
        assets=assets, gold=gold,
        expects={"intent": "query", "has_result": expect_result},
        note=f"财务指标 {STD_TO_LABEL.get(metric)} @{name}",
    )


def _mk_market_case(rng: random.Random, n: int, complexity: str) -> EvalCase:
    """行情单要素 (股价/涨跌/PE/PB/市值)"""
    stock = rng.choice(STOCK_POOL)
    name, code, ind = stock
    metric = rng.choice(list(MKT_METRIC_ALIASES.keys()))
    alias = rng.choice(MKT_METRIC_ALIASES[metric])
    verb = rng.choice(["查询", "查一下", "请问", "看看", "帮我查", "股价", ""])
    if verb == "股价" and metric != "mkt_d_close":
        verb = "查询"
    # daily_basic 指标无时间 (数据1991-2011断档, 问2023无意义) -> 一律不带时间
    is_db = metric.startswith("mkt_db")
    time_expr = ""
    if (not is_db) and rng.random() < 0.25:
        time_expr, _ = rng.choice(TIME_PHRASES[:8])
    q = f"{verb}{name}{time_expr}的{alias}" if time_expr else f"{verb}{name}的{alias}"
    gold = {
        "stocks": [{"name": name, "ts_code": code}],
        "metrics": [metric],
        "time": None,
        "conditions": [],
    }
    return mk_case(
        f"mkt_{n:04d}", q, "query", complexity, "market",
        assets=["market", "db_missing"] if is_db else ["market"],
        gold=gold,
        expects={"intent": "query", "has_result": True if not is_db else "maybe"},
        note=f"行情指标 {STD_TO_LABEL.get(metric)} @{name}",
    )


def _mk_alias_case(rng: random.Random, n: int, complexity: str) -> EvalCase:
    """别名识别"""
    name, code, alias, ind = _pick_stock_with_alias(rng, STOCK_POOL, need_alias=True)
    # 指标选择: 别名股大多是行情股, 财务仅 fina_indicator 可靠
    # 为保证能查 (行情 daily 或 fina_indicator), 优先行情 daily + fin_f_
    metric = rng.choice(
        list(MKT_METRIC_ALIASES.keys()) +
        ["fin_f_roe", "fin_f_netprofit_margin", "fin_f_grossprofit_margin",
         "fin_f_debt_to_assets", "fin_f_netprofit_yoy", "fin_f_or_yoy",
         "fin_f_current_ratio", "fin_f_quick_ratio"]
    )
    alias_m = rng.choice(MKT_METRIC_ALIASES.get(metric, FIN_METRIC_ALIASES.get(metric, ["营收"])))
    verb = rng.choice(["查一下", "看看", "帮我查", "请问"])
    q = f"{verb}{alias}的{alias_m}"
    is_db = metric in ("mkt_db_pe_ttm", "mkt_db_pb", "mkt_db_total_mv", "mkt_db_circ_mv", "mkt_db_turnover_rate")
    fin3_codes = {c for _, c, _ in FIN3}
    gold = {
        "stocks": [{"name": alias, "std_name": name, "ts_code": code}],
        "metrics": [metric],
        "time": None,
        "conditions": [],
    }
    if is_db:
        res = "maybe"
    elif metric.startswith("fin_f_") or metric.startswith("fin_i_") or \
         metric.startswith("fin_b_") or metric.startswith("fin_c_"):
        res = metric_has_data_or_none(code, metric)
    else:
        res = True
    assets = ["market" if not metric.startswith("fin") else "fin"]
    if is_db:
        assets.append("db_missing")
    return mk_case(
        f"alias_{n:04d}", q, "query", complexity, "aliases",
        assets=assets,
        gold=gold,
        expects={"intent": "query", "has_result": res},
        note=f"别名词典: {alias}->{name}",
    )


def _mk_compare_case(rng: random.Random, n: int, complexity: str) -> EvalCase:
    """多股对比"""
    s1, s2 = rng.sample(STOCK_POOL, 2)
    name1, code1, ind1 = s1
    name2, code2, ind2 = s2
    # 指标
    metric_pool = list(FIN_METRIC_ALIASES.keys()) + ["mkt_db_pe_ttm", "mkt_db_pb", "mkt_db_total_mv"]
    metric = rng.choice(metric_pool)
    alias = rng.choice(FIN_METRIC_ALIASES.get(metric, MKT_METRIC_ALIASES.get(metric, ["营收"])))
    # 主语用"哪个/谁的"则句子自然
    if rng.random() < 0.55:
        tmpl = rng.choice(COMPARE_WHICH)
        q = tmpl.format(s1=name1, s2=name2, metric=alias)
    else:
        cj = rng.choice(COMPARE_JOIN)
        verb = rng.choice(["对比", "比较", "查一下", "看看"])
        q = f"{verb}{name1}{cj}{name2}的{alias}"
    gold = {
        "stocks": [{"name": name1, "ts_code": code1}, {"name": name2, "ts_code": code2}],
        "metrics": [metric],
        "time": None,
        "conditions": [],
    }
    # 判定: 查询库断言 (个股×字段) 优于 表级推断
    is_db = metric in ("mkt_db_pe_ttm", "mkt_db_pb", "mkt_db_total_mv", "mkt_db_circ_mv", "mkt_db_turnover_rate")
    if is_db:
        res = "maybe"  # daily_basic 断档
    elif metric.startswith("fin"):
        # 对比语义需要双方数据 (至少一方; 严格双股)
        a1 = metric_has_data(code1, metric)
        a2 = metric_has_data(code2, metric)
        if a1 is False and a2 is False:
            res = False
        elif a1 is True and a2 is False:
            res = "maybe"   # 一方有数据, 对比结果不完整
        else:
            res = True
    else:
        res = True  # daily 行情双方都有
    assets = ["fin" if metric.startswith("fin") else "market"]
    if is_db:
        assets.append("db_missing")
    return mk_case(
        f"cmp_{n:04d}", q, "compare", complexity, "compare",
        assets=assets,
        gold=gold,
        expects={"intent": "compare", "has_result": res},
        note=f"对比 {name1} vs {name2} ({STD_TO_LABEL.get(metric)})",
    )


def _mk_rank_case(rng: random.Random, n: int, complexity: str) -> EvalCase:
    """排名/条件筛选"""
    mode = rng.random()
    if mode < 0.5:
        # 排名 TopN
        alias, n_rank = rng.choice(RANK_COMBOS)
        # 指标标准化
        metric = {
            "ROE": "roe", "毛利率": "grossprofit_margin", "净利率": "netprofit_margin",
            "营收": "revenue", "净利润": "n_income_attr_p", "市值": "total_mv",
            "PE": "pe_ttm", "PB": "pb",
        }[alias]
        pattern, ptype = rng.choice(RANK_PATTERNS)
        # 50% 带行业限定
        ind_choice = None
        if rng.random() < 0.5:
            ind_choice = rng.choice(["白酒", "银行", "半导体", "元器件", "家用电器"])
            inner = pattern.format(alias=alias, n=n_rank)
            q = f"{ind_choice}板块中{inner}"
        else:
            q = f"全市场{pattern.format(alias=alias, n=n_rank)}"
        gold = {
            "stocks": [],
            "metrics": [metric],
            "time": None,
            "conditions": [{"type": "rank", "top": int(n_rank), "by": metric, "industry": ind_choice}],
        }
        return mk_case(
            f"rank_{n:04d}", q, "query", "hard", "rank",
            assets=["fin", "market"], gold=gold,
            expects={"intent": "query", "has_result": "maybe"},
            note=f"排名查询 {metric} Top{n_rank}",
        )
    # 条件筛选
    metric = rng.choice(["roe", "grossprofit_margin", "netprofit_margin", "debt_to_assets", "netprofit_yoy"])
    metric_alias = {
        "roe": "ROE", "grossprofit_margin": "毛利率", "netprofit_margin": "净利率",
        "debt_to_assets": "资产负债率", "netprofit_yoy": "净利润同比",
    }[metric]
    cond_word, cond_kind = rng.choice(COND_TEMPLATES)
    threshold = rng.choice(THRESHOLD_CANDIDATES[metric])
    # 行业可选
    ind = rng.choice(["白酒", "银行", "半导体", "元器件", "家用电器", "汽车整车", "电气设备"]) if rng.random() < 0.7 else None
    if ind:
        q = f"{ind}板块中{metric_alias}{cond_word}{threshold}的股票"
    else:
        q = f"找出{metric_alias}{cond_word}{threshold}的股票"
    gold = {
        "stocks": [],
        "metrics": [metric],
        "time": None,
        "conditions": [{"type": "filter", "metric": metric, "op": cond_kind, "value": threshold, "industry": ind}],
    }
    return mk_case(
        f"filter_{n:04d}", q, "query", "hard", "rank",
        assets=["fin", "market", "db_missing" if metric == "pe_ttm" else ""],
        gold=gold,
        expects={"intent": "query", "has_result": "maybe"},
        note=f"条件筛选 {metric}{cond_kind}{threshold}",
    )


def _mk_combined_case(rng: random.Random, n: int, complexity: str) -> EvalCase:
    """单股多指标 + 时间范围"""
    name, code = rng.choice(FIN3)[:2]
    # 指标: 优先选该股有数据的字段
    fin_metrics = list(FIN_METRIC_ALIASES.keys())
    rng.shuffle(fin_metrics)
    has = [m for m in fin_metrics if metric_has_data_or_none(code, m)]
    if len(has) < 2:
        has = fin_metrics  # 保底 (极端情况查库失败)
    m1, m2 = has[:2]
    a1 = rng.choice(FIN_METRIC_ALIASES[m1])
    a2 = rng.choice(FIN_METRIC_ALIASES[m2])
    # 时间: 统一结构化 (kind, value)
    time_expr, time_meta = rng.choice(TIME_PHRASES[:8])
    if rng.random() < 0.5:
        # 范围: 用 2021-2023 样式
        y1 = rng.choice(["2020", "2021", "2022"])
        y2 = rng.choice(["2023", "2024"])
        if y1 < y2:
            time_expr, time_meta = f"{y1}-{y2}年", ("range", (int(y1), int(y2)))
    if time_expr:
        q = f"{name}{time_expr}的{a1}和{a2}"
    else:
        q = f"{name}的{a1}和{a2}"
    gold = {
        "stocks": [{"name": name, "ts_code": code}],
        "metrics": [m1, m2],
        "time": time_meta,
        "conditions": [],
    }
    return mk_case(
        f"combi_{n:04d}", q, "query", complexity, "combined",
        assets=["fin"], gold=gold,
        expects={"intent": "query", "has_result": True},
        note=f"{name} {a1}+{a2}",
    )


def _mk_detect_case(rng: random.Random, n: int, complexity: str) -> EvalCase:
    """异常检测"""
    name, code = rng.choice(FIN3)[:2]
    q = rng.choice([
        f"分析{name}的财务风险",
        f"{name}有没有财务造假风险？",
        f"帮我排查一下{name}的财务异常",
        f"{name}最近有没有异常",
        f"检测{name}的财报异常信号",
    ])
    gold = {"stocks": [{"name": name, "ts_code": code}], "metrics": [], "time": None, "conditions": []}
    return mk_case(
        f"detect_{n:04d}", q, "detect", complexity, "detect",
        assets=["fin"], gold=gold,
        expects={"intent": "detect", "has_result": False},
        note=f"异常检测 {name}",
    )


def _mk_verify_case(rng: random.Random, n: int) -> EvalCase:
    """研报校验"""
    name, code = rng.choice(FIN3)[:2]
    q = rng.choice([
        f"验证{name}的研报有没有问题",
        f"帮我核对{name}的最新研报数据",
        f"{name}的券商研报预测和实际一致吗",
    ])
    gold = {"stocks": [{"name": name, "ts_code": code}], "metrics": [], "time": None, "conditions": []}
    return mk_case(
        f"verify_{n:04d}", q, "verify", "hard", "verify",
        assets=["fin"], gold=gold,
        expects={"intent": "verify", "has_result": False},
        note=f"研报校验 {name}",
    )


def _mk_report_case(rng: random.Random, n: int) -> EvalCase:
    """报告生成"""
    name, code = rng.choice(FIN3)[:2]
    q = rng.choice([
        f"生成{name}的深度分析报告",
        f"给{name}写一份完整的研究报告",
        f"帮我生成{name}的分析报告",
    ])
    gold = {"stocks": [{"name": name, "ts_code": code}], "metrics": [], "time": None, "conditions": []}
    return mk_case(
        f"report_{n:04d}", q, "report", "hard", "report",
        assets=["fin"], gold=gold,
        expects={"intent": "report", "has_result": False},
        note=f"报告生成 {name}",
    )


def _mk_attack_case(n: int, q: str, intent: str, cat: str, note: str) -> EvalCase:
    """攻击/边界"""
    expects = {"intent": intent, "has_result": False}
    if cat == "attack":
        expects["should_block"] = True   # 危险SQL应被拦截
    return mk_case(
        f"attack_{n:04d}" if cat == "attack" else f"edge_{n:04d}",
        q, intent, "attack", cat, assets=[], gold={},
        expects=expects, note=note,
    )


# ============================================================
# 生成器主函数
# ============================================================

def generate(n: int = 1000, seed: int = 42) -> List[EvalCase]:
    rng = random.Random(seed)
    cases: List[EvalCase] = []

    # 分层权重 (对齐真实用户分布)
    # 约: financial 30% / market 20% / alias 8% / combine 8% / compare 12%
    #     rank+filter 10% / detect 3% / verify 1% / report 1% / attack+edge 7%
    plan = []
    p_fin = int(n * 0.30)
    p_mkt = int(n * 0.20)
    p_alias = int(n * 0.08)
    p_cmb = int(n * 0.08)
    p_cmp = int(n * 0.12)
    p_rank = int(n * 0.10)
    p_det = int(n * 0.03)
    p_ver = int(n * 0.01)
    p_rep = int(n * 0.01)
    p_att = int(n * 0.07)
    plan = {
        "financial": p_fin, "market": p_mkt, "alias": p_alias, "combined": p_cmb,
        "compare": p_cmp, "rank": p_rank, "detect": p_det, "verify": p_ver,
        "report": p_rep, "attack": p_att,
    }

    # 分组生成
    for i in range(plan["financial"]):
        cases.append(_mk_financial_case(rng, i, rng.choice(["easy", "normal", "easy", "normal"])))
    for i in range(plan["market"]):
        cases.append(_mk_market_case(rng, i, rng.choice(["easy", "normal"])))
    for i in range(plan["alias"]):
        cases.append(_mk_alias_case(rng, i, rng.choice(["normal", "hard"])))
    for i in range(plan["combined"]):
        cases.append(_mk_combined_case(rng, i, "normal"))
    for i in range(plan["compare"]):
        cases.append(_mk_compare_case(rng, i, rng.choice(["normal", "hard"])))
    for i in range(plan["rank"]):
        cases.append(_mk_rank_case(rng, i, "hard"))
    for i in range(plan["detect"]):
        cases.append(_mk_detect_case(rng, i, "hard"))
    for i in range(plan["verify"]):
        cases.append(_mk_verify_case(rng, i))
    for i in range(plan["report"]):
        cases.append(_mk_report_case(rng, i))
    # attack+edge 固定语料循环补足
    for i in range(plan["attack"]):
        q, intent, cat, note = ATTACK_CASES[i % len(ATTACK_CASES)]
        cases.append(_mk_attack_case(i // len(ATTACK_CASES) * 1000 + i % len(ATTACK_CASES),
                                     q, intent, cat, note))

    # 补足到 n
    i_ext = len(cases)
    while len(cases) < n:
        cases.append(_mk_financial_case(rng, i_ext, "normal"))
        i_ext += 1

    cases = cases[:n]
    # 洗牌 (保留可复现)
    rng.shuffle(cases)
    return cases


def validate_dataset(cases: List[EvalCase]) -> Dict[str, Any]:
    """
    数据集完整性自检 (生成后验收门禁):
    1. 重复问句率 (应 < 25%)
    2. 标注断言可执行性 (expects 字段完整)
    3. 句子自然度 (无占位符/拼接异常)
    4. 覆盖完整性 (各分类都有, 难度/意图/指标覆盖)
    5. 资产一致性 (db_missing 不配 expects=True)
    """
    issues: List[str] = []
    qs = [c.question for c in cases]
    dup = len(qs) - len(set(qs))
    dup_rate = dup / len(qs)
    if dup_rate > 0.25:
        issues.append(f"重复问句率 {dup_rate*100:.1f}% > 25%")

    # 3. 自然度检查
    BAD_PATTERNS = [
        ("最高的几个10", "排名组合异常"),
        (" vs.(", "vs语法"),
        ("的的", "重复的"),
        ("（）", "空括号"),
        ("{}", "占位符"),
        ("None", "None残留"),
    ]
    for c in cases:
        for pat, desc in BAD_PATTERNS:
            if pat in c.question:
                issues.append(f"[{desc}] '{c.question}' 含 '{pat}'")

    # 4. 覆盖完整性
    cats = [c.category for c in cases]
    need_cats = {"financial", "market", "compare", "rank", "aliases", "combined",
                 "edge", "attack", "detect"}
    missing = need_cats - set(cats)
    if missing:
        issues.append(f"缺少分类: {missing}")
    cpx = [c.complexity for c in cases]
    if not {"easy", "normal", "hard", "attack"} <= set(cpx):
        issues.append(f"难度覆盖不全: {set(cpx)}")

    # 5. 资产一致性
    for c in cases:
        e = c.expects.get("has_result")
        if e is True and "db_missing" in c.assets:
            issues.append(f"[资产冲突] '{c.question}' db_missing 却 expects=True")
        if e is False and not (c.assets or c.category in ("edge", "attack", "detect", "verify", "report")):
            issues.append(f"[资产冲突] '{c.question}' expects=False 且无资产类别")

    # 6. 标注断言完备性
    for c in cases:
        if "has_result" not in c.expects:
            issues.append(f"[标注缺失] '{c.question}' 无 has_result")
        if c.intent not in ("query", "compare", "detect", "verify", "report", "unknown"):
            issues.append(f"[意图非法] '{c.question}' intent={c.intent}")

    # 7. 指标覆盖 (财报+行情+商业指标)
    metrics_used = set()
    for c in cases:
        for m in c.gold.get("metrics", []):
            metrics_used.add(m)
    if len(metrics_used) < 25:
        issues.append(f"指标覆盖仅 {len(metrics_used)} < 25")

    # 8. gold.stocks 与 question 一致性 (含别名的 token 应出现在问题中)
    for c in cases:
        for s in c.gold.get("stocks", []):
            nm = s.get("name") or s.get("std_name")
            if nm and nm not in c.question and s.get("std_name") not in c.question:
                issues.append(f"[gold不一致] '{c.question}' 标注股票 {nm} 未出现在问题中")

    return {
        "total": len(cases),
        "unique_questions": len(set(qs)),
        "dup_rate": round(dup_rate, 4),
        "metrics_covered": len(metrics_used),
        "problems": len(issues),
        "issues": issues[:20],
    }


def main(n: int = 1000, seed: int = 42, save: bool = True):
    cases = generate(n, seed)
    # 统计
    by_cat = Counter(c.category for c in cases)
    by_cpx = Counter(c.complexity for c in cases)
    by_intent = Counter(c.intent for c in cases)
    uq = len(set(c.question for c in cases))
    print("=" * 60)
    print(f"生成 {len(cases)} 条 (唯一问法 {uq}, {uq/len(cases)*100:.1f}%)")
    print("分类:", dict(by_cat))
    print("难度:", dict(by_cpx))
    print("意图:", dict(by_intent))

    # 资产覆盖
    assets = Counter()
    for c in cases:
        for a in c.assets:
            if a:
                assets[a] += 1
    print("资产:", dict(assets))

    # 指标覆盖
    metrics_used = Counter()
    for c in cases:
        for m in c.gold.get("metrics", []):
            metrics_used[m] += 1
    print("指标覆盖数:", len(metrics_used))

    # 完整性自检
    v = validate_dataset(cases)
    print("-" * 60)
    print(f"自检: 重复率 {v['dup_rate']*100:.1f}%, 指标覆盖 {v['metrics_covered']}, 问题 {v['problems']} 个")
    for i in v["issues"]:
        print(f"  ⚠ {i}")
    if v["problems"]:
        print("!! 数据集存在质量问题, 请修复后再用于评测")

    if save:
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = OUT_DIR / f"eval_dataset_v2_{n}_{seed}.json"
        payload = {
            "meta": {
                "n": len(cases), "seed": seed, "version": "v2",
                "by_category": dict(by_cat), "by_complexity": dict(by_cpx),
                "by_intent": dict(by_intent), "unique_questions": uq,
                "validate": v,
            },
            "cases": [c.to_dict() for c in cases],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n已保存: {path}")
    return cases


if __name__ == "__main__":
    import time
    ap = argparse.ArgumentParser("生成评测数据集 v2")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    main(args.n, args.seed, not args.no_save)