# -*- encoding: utf-8 -*-
"""T06 引用校验器测试 — 纯离线 (不连 LLM/DB), 构造样例覆盖四类检查。

分组:
  test_extract / test_iter       引用提取与句子切分基础行为
  test_unverified_*              数字句启发 (有引用/无引用/年份豁免/待验证豁免/标题表格跳过)
  test_ghost_*                   幽灵引用 (正文/used_eids/假设 related_eids)
  test_used_eids_*               used_eids 与正文一致性
  test_view_style_*              观点句宽松提示
  test_check_draft               全稿入口: 去重保序、聚合上限
"""
from skills.research.compose.citation_check import (
    check_draft,
    check_ghost_citations,
    check_unverified_numbers,
    check_used_eids_consistency,
    check_view_style,
    extract_citations,
    iter_sentences,
)
from skills.research.contracts import Chapter, Draft, Fact, Hypothesis, View

# ---------- fixtures ----------

FACTS = [
    Fact(eid="T:tushare.revenue.2025A", content="2025年营业收入1741亿元",
         source="fin.income.revenue", period="2025A", value=1741.0, unit="亿元"),
    Fact(eid="A:2025.mdna.1", content="2025年茅台酒基酒产量约5.6万吨",
         source="2025年报·mdna·三、经营情况", period="2025A", value=5.6, unit="万吨"),
]
VIEWS = [
    View(vid="R:101.1", statement="预计2026年营收增长10%", org="中金",
         publish_date="2026-03-01", period="2026A"),
    View(vid="R:102.1", statement="维持买入评级", org="国信",
         publish_date="2026-02-14", period="2026A"),
]
EID_SET = {f.eid for f in FACTS}
VID_SET = {v.vid for v in VIEWS}


def _draft(**kw) -> Draft:
    """默认合法草稿: 第1章正文引用齐全, used_eids 与正文一致。"""
    body = kw.pop("body", "2025年营业收入1741亿元[E:T:tushare.revenue.2025A]。"
                          "中金(2026-03-01)认为需求稳健[V:R:101.1]。")
    used = kw.pop("used_eids", ["T:tushare.revenue.2025A", "R:101.1"])
    return Draft(ts_code="600519.SH", name="贵州茅台",
                 chapters=[Chapter(no=1, title="公司定位", body_md=body, used_eids=used)],
                 **kw)


# ---------- 基础行为 ----------

def test_extract_citations_ordered_with_dup():
    text = "甲[E:A:2025.mdna.1]乙[V:R:101.1]丙[E:A:2025.mdna.1]"
    assert extract_citations(text) == [("E", "A:2025.mdna.1"), ("V", "R:101.1"),
                                       ("E", "A:2025.mdna.1")]


def test_iter_sentences_skips_heading_and_table():
    md = "### 2025年分产品收入\n| 产品 | 收入 |\n|---|---|\n| 茅台酒 | 1459 |\n正文一句。第二句。"
    assert list(iter_sentences(md)) == ["正文一句", "第二句"]


# ---------- 数字句启发 ----------

def test_unverified_number_flagged():
    flags = check_unverified_numbers(1, "净利润达862亿元。")
    assert len(flags) == 1 and flags[0].startswith("unverified-number: 第1章「")


def test_cited_number_not_flagged():
    assert check_unverified_numbers(1, "净利润862亿元[E:T:tushare.revenue.2025A]。") == []


def test_year_only_sentence_not_flagged():
    assert check_unverified_numbers(1, "本报告覆盖2021-2025年的经营历史。") == []
    assert check_unverified_numbers(1, "公司于1999年上市, 2019年起深化渠道改革。") == []


def test_period_token_not_flagged():
    assert check_unverified_numbers(1, "2025Q3营收创单季新高, 2025H1同比改善。") == []


def test_pending_verification_exemption():
    assert check_unverified_numbers(1, "渠道库存约2个月(待验证)。") == []


def test_cited_by_view_not_flagged_by_number_heuristic():
    """句内已有 [V:] 标注 → 数字句启发不再重复告警 (归观点规范检查负责)。"""
    assert check_unverified_numbers(1, "2025年净利润862亿元[V:R:101.1]。") == []


def test_unverified_flags_aggregate_over_cap():
    body = "。".join(f"第{i}项指标为{i}亿元" for i in range(1, 13)) + "。"
    flags = check_unverified_numbers(2, body)
    assert len(flags) == 9      # 8 条样例 + 1 条聚合 (12 - 8 = 4 句省略)


# ---------- 幽灵引用 ----------

def test_ghost_body_citation():
    flags = check_ghost_citations(_draft(body="销量大增[E:T:ghost.1]。"), EID_SET, VID_SET)
    assert any("ghost-citation" in f and "[E:T:ghost.1]" in f for f in flags)


def test_ghost_used_eids():
    flags = check_ghost_citations(_draft(used_eids=["R:999.9"]), EID_SET, VID_SET)
    assert any("used_eids 引用 'R:999.9'" in f for f in flags)


def test_ghost_hypothesis_related_eids():
    d = _draft(hypotheses=[Hypothesis(hid="H1", statement="s", indicators=["i"],
                                      related_eids=["T:ghost.9", "T:tushare.revenue.2025A"])])
    flags = check_ghost_citations(d, EID_SET, VID_SET)
    assert len(flags) == 1 and "假设 H1" in flags[0]


def test_no_ghost_on_valid_draft():
    assert check_ghost_citations(_draft(), EID_SET, VID_SET) == []


# ---------- used_eids 一致性 ----------

def test_used_eids_extra_and_missing():
    d = _draft(used_eids=["T:tushare.revenue.2025A"])     # 漏记 R:101.1
    d.chapters[0].used_eids.append("A:2025.mdna.1")       # 多记正文未引用
    flags = check_used_eids_consistency(d)
    assert any("正文引用有 1 项未记入" in f for f in flags)
    assert any("used_eids 有 1 项正文未引用" in f for f in flags)


# ---------- 观点句宽松提示 ----------

def test_view_style_with_org_passes():
    assert check_view_style(1, "中金(2026-03-01)认为需求稳健[V:R:101.1]。", VIEWS) == []


def test_view_style_with_judge_word_passes():
    assert check_view_style(1, "高端白酒需求预期稳健[V:R:101.1]。", VIEWS) == []


def test_view_style_without_org_hinted():
    flags = check_view_style(1, "渠道库存处于合理水平[V:R:101.1]。", VIEWS)
    assert len(flags) == 1 and flags[0].startswith("view-style-hint: 第1章")


# ---------- 全稿入口 ----------

def test_check_draft_combines_and_dedups():
    d = _draft(body="净利润862亿元。净利润862亿元。渠道无引用[V:R:404.9]。")
    d.chapters[0].used_eids = ["T:tushare.revenue.2025A", "R:101.1"]
    flags = check_draft(d, FACTS, VIEWS)
    assert any(f.startswith("unverified-number") for f in flags)
    assert any(f.startswith("ghost-citation") and "R:404.9" in f for f in flags)
    assert any(f.startswith("used-eids-mismatch") for f in flags)
    assert len(flags) == len(set(flags))       # 去重: 两个相同数字句只留一条
