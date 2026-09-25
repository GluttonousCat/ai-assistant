# -*- encoding: utf-8 -*-
"""T07 图表工场离线测试 — Agg 后端, 不连 LLM/DB。

分组:
  test_route   分类规则与 eid 指标键解析 (含优先级/词边界防误吞)
  test_build   茅台形态 fixture → ≥5 图/章绑定/命名规范/文件非空
  test_filter  draft 章节过滤 (只出能落进现有章的图, 不写孤儿文件)
  test_minimal 最小 fixture → ≥3 类图; 空证据不炸
"""
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from skills.research.compose.figures import (  # noqa: E402
    FigureOut,
    _classify,
    _series_key,
    build_figures,
)
from skills.research.contracts import Chapter, Draft, Fact  # noqa: E402

_TS = "600519.SH"
_NAME_RE = re.compile(r"^ch\d{2}_[a-z0-9_]+\.png$")


def _f(eid, content, source, period, value, unit):
    return Fact(eid=eid, content=content, source=source, period=period,
                value=value, unit=unit, ts_code=_TS)


def maotai_facts():
    """茅台形态 fixture (数值为量级合理的示例, 仅供离线绘图测试)"""
    facts = []
    rev = [1094.64, 1275.54, 1505.60, 1741.44, 1871.90]
    prof = [524.60, 627.16, 747.34, 862.28, 941.60]
    ocf = [640.29, 756.89, 965.47, 1120.51, 1215.30]
    gross = [91.54, 91.87, 91.96, 91.93, 92.05]
    netm = [47.92, 49.17, 49.64, 49.52, 50.30]
    roe = [29.90, 31.75, 34.19, 36.24, 37.50]
    prod = [5.65, 5.68, 5.72, 5.63, 5.70]
    sales = [3.63, 3.80, 4.01, 4.15, 4.30]
    rd = [1.74, 2.20, 2.51, 2.80, 3.02]
    for i, y in enumerate(range(2021, 2026)):
        p = f"{y}A"
        facts += [
            _f(f"T:tushare.revenue.{p}", f"{y}年营业收入{rev[i]}亿元",
               "fin.income.revenue (Tushare)", p, rev[i], "亿元"),
            _f(f"T:tushare.n_income_attr_p.{p}", f"{y}年归母净利润{prof[i]}亿元",
               "fin.income.n_income_attr_p (Tushare)", p, prof[i], "亿元"),
            _f(f"T:tushare.netcash_operate.{p}", f"{y}年经营活动现金流净额{ocf[i]}亿元",
               "fin.cashflow.netcash_operate (Tushare)", p, ocf[i], "亿元"),
            _f(f"T:tushare.grossprofit_margin.{p}", f"{y}年毛利率{gross[i]}%",
               "fin.indicator.grossprofit_margin (Tushare)", p, gross[i], "%"),
            _f(f"T:tushare.netprofit_margin.{p}", f"{y}年净利率{netm[i]}%",
               "fin.indicator.netprofit_margin (Tushare)", p, netm[i], "%"),
            _f(f"T:tushare.roe.{p}", f"{y}年ROE为{roe[i]}%",
               "fin.indicator.roe (Tushare)", p, roe[i], "%"),
            _f(f"T:annual.基酒产量.{p}", f"{y}年茅台酒基酒产量约{prod[i]}万吨",
               f"{y}年报·经营情况讨论与分析", p, prod[i], "万吨"),
            _f(f"T:annual.sales_volume.{p}", f"{y}年茅台酒销量约{sales[i]}万吨",
               f"{y}年报·经营情况讨论与分析", p, sales[i], "万吨"),
            _f(f"T:tushare.rd_expense.{p}", f"{y}年研发费用{rd[i]}亿元",
               "fin.income.rd_exp (Tushare)", p, rd[i], "亿元"),
        ]
    # 主营构成 (最新期间, 三项)
    for item, share in (("茅台酒", 83.9), ("系列酒", 14.5), ("其他业务", 1.6)):
        facts.append(_f(f"T:mainbiz.{item}.2025A", f"2025年{item}收入占比{share}%",
                        "主营业务构成 (Tushare)", "2025A", share, "%"))
    # 估值月度样本 (12 点 PE + 12 点 PB)
    for m in range(1, 13):
        d = f"2025-{m:02d}-15"
        facts.append(_f(f"T:valuation.pe_ttm.{d}", f"{d}市盈率PE(TTM)",
                        "daily_basic.pe_ttm (Tushare)", d,
                        21.5 + 0.63 * (m - 1), ""))
        facts.append(_f(f"T:valuation.pb.{d}", f"{d}市净率PB",
                        "daily_basic.pb (Tushare)", d,
                        7.9 + 0.17 * (m - 1), ""))
    return facts


# ---------- test_route: 分类与指标键 ----------

def test_series_key_strips_period():
    assert _series_key(_f("T:tushare.revenue.2024A", "x", "s", "2024A", 1, "")) \
        == "tushare.revenue"
    assert _series_key(_f("A:2024.mdna.3.1", "x", "s", "2024A", 1, "")) \
        == "mdna.3.1"


def test_classify_priority_and_boundary():
    assert _classify("tushare.netprofit_margin") == "netmargin"   # 不被 profit 吞
    assert _classify("tushare.n_income_attr_p") == "profit"
    assert _classify("tushare.capex") is None                     # capex 不误判为 pe
    assert _classify("valuation.pe_ttm") == "pe"
    assert _classify("mdna.基酒产量", "2024年茅台酒基酒产量5.72万吨") == "volume"
    assert _classify("mainbiz.茅台酒") == "mainbiz"


# ---------- test_build: 茅台形态全量 ----------

def test_maotai_full_fixture_five_figs(tmp_path):
    figs = build_figures(Draft(ts_code=_TS, name="贵州茅台"),
                         maotai_facts(), tmp_path)
    # ≥5 张图, 覆盖 ch02/ch04/ch05/ch10 (guide 章节输出要求)
    assert len(figs) >= 5, [f.path.name for f in figs]
    chs = {f.chapter_no for f in figs}
    assert {2, 4, 5, 10} <= chs
    for f in figs:
        assert _NAME_RE.match(f.path.name), f.path.name
        assert int(f.path.name[2:4]) == f.chapter_no     # 文件名前缀=章号
        assert f.path.exists() and f.path.stat().st_size > 5000
        assert f.hint.startswith("章末插入")              # T08 组装提示
    # 四类图各就各位
    names = {f.path.name for f in figs}
    assert {"ch02_mainbiz.png", "ch04_growth.png", "ch04_margins.png",
            "ch04_np_ocf.png", "ch05_volume.png", "ch10_valuation.png"} <= names


def test_figureout_unpacks_triple():
    p, no, hint = FigureOut(Path("a.png"), 4, "章末插入 · x")
    assert (no, hint) == (4, "章末插入 · x") and p.name == "a.png"


# ---------- test_filter: draft 章节过滤 ----------

def test_draft_chapter_filter(tmp_path):
    draft = Draft(ts_code=_TS, name="贵州茅台",
                  chapters=[Chapter(no=i, title=f"第{i}章", body_md="")
                            for i in (1, 2, 3, 4)])
    figs = build_figures(draft, maotai_facts(), tmp_path)
    assert figs and {f.chapter_no for f in figs} <= {1, 2, 3, 4}
    # 被过滤的章不落孤儿文件
    assert not (tmp_path / "figs" / "ch10_valuation.png").exists()
    assert not (tmp_path / "figs" / "ch05_volume.png").exists()


# ---------- test_minimal ----------

def test_minimal_three_types(tmp_path):
    facts = [
        _f(f"T:tushare.revenue.{y}A", f"{y}年营业收入{x}亿元",
           "fin.income.revenue", f"{y}A", x, "亿元")
        for y, x in ((2023, 1505.6), (2024, 1741.4), (2025, 1871.9))
    ]
    facts += [
        _f("T:mainbiz.茅台酒.2025A", "2025年茅台酒收入占比83.9%", "主营构成",
           "2025A", 83.9, "%"),
        _f("T:mainbiz.系列酒.2025A", "2025年系列酒收入占比14.5%", "主营构成",
           "2025A", 14.5, "%"),
    ]
    facts += [
        _f(f"T:valuation.pe_ttm.2025-{m:02d}-15", "市盈率", "daily_basic.pe_ttm",
           f"2025-{m:02d}-15", 22.0 + m, "")
        for m in range(1, 7)
    ]
    figs = build_figures(Draft(ts_code=_TS, name="茅台"), facts, tmp_path)
    assert len(figs) >= 3, [f.path.name for f in figs]
    assert {f.path.name for f in figs} >= {"ch02_mainbiz.png", "ch04_growth.png",
                                           "ch10_valuation.png"}


def test_empty_and_unrecognized_facts(tmp_path):
    assert build_figures(Draft(ts_code="X", name="X"), [], tmp_path) == []
    # 无可归类时序 → 不出图不炸 (只有单点/非数值)
    odd = [
        _f("T:other.solo.2025A", "2025年某单点指标", "s", "2025A", 3.3, "x"),
        _f("T:tushare.revenue.2025A", "无 value", "s", "2025A", None, ""),
    ]
    assert build_figures(Draft(ts_code="X", name="X"), odd, tmp_path) == []
