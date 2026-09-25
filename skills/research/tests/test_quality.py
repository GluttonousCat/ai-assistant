# -*- encoding: utf-8 -*-
"""T09 质量评分卡离线测试 — fixture 构造满分/踩线/单项失败样例, 不连 LLM/DB。

覆盖: score() 六维达标线边界、幽灵引用三来源 (正文/used_eids/flags)、跳章豁免、
句子枚举口径 (表格/标题/代码行/交叉引用噪声)、机构核心词归属、score_from_dir
落盘 roundtrip、与 T01 validate_draft 的交叉一致性。
"""
from pathlib import Path

import pytest

from skills.research import quality_rubric as qr
from skills.research.contracts import (
    Chapter,
    Draft,
    Fact,
    Hypothesis,
    View,
    save_draft,
    save_shard,
    validate_draft,
)


# ---------- fixture 构造 ----------

def mk_facts(total: int = 105, annual: int = 16):
    facts = []
    for i in range(annual):
        facts.append(Fact(eid=f"A:2025.mdna.s{i + 1}",
                          content=f"2025年经营数据第{i + 1}项为{i + 1}万吨",
                          source=f"2025年报·管理层讨论·小节{i + 1}",
                          period="2025A", value=float(i + 1), unit="万吨"))
    for i in range(max(total - annual, 0)):
        facts.append(Fact(eid=f"T:tushare.metric{i}.2021A",
                          content=f"指标{i}为{i}亿元",
                          source=f"fin.income.metric{i}",
                          period="2021A", value=float(i), unit="亿元"))
    return facts


VIEWS = [
    View(vid="R:101.1", statement="高端白酒景气延续", org="中金公司",
         publish_date="2026-03-15", category="viewpoint"),
    View(vid="R:102.1", statement="上调盈利预测", org="国泰君安证券",
         publish_date="2026-04-01", category="forecast"),
]
VIEW_BY_VID = {v.vid: v for v in VIEWS}

CH_TITLES = {i: f"第{i}章" for i in range(1, 11)}


def mk_chapter(no: int, sents, used_eids=None):
    return Chapter(no=no, title=CH_TITLES[no], body_md="\n".join(sents),
                   used_eids=used_eids or [])


def full_chapter(no: int):
    """标准好章: 2 个数字句全部带引用 (1 个 [E:] + 1 个有归属的 [V:]), 1 句定性"""
    return mk_chapter(no, [
        f"公司营业收入为{100 + no}亿元 [E:T:tushare.metric{no}.2021A]",
        "## 一、经营概览",                      # 标题行不计句子
        "| 年份 | 营收 |\n|---|---|\n| 2021 | 100 |",  # 表格行不计句子
        "经营质量保持稳健",                       # 非数字句不进覆盖率分母
        "中金公司(2026-03-15)认为需求将增长15% [V:R:101.1]",
    ], used_eids=[f"T:tushare.metric{no}.2021A", "R:101.1"])


def full_draft():
    return Draft(
        ts_code="600519.SH", name="贵州茅台",
        chapters=[full_chapter(i) for i in range(1, 11)],
        hypotheses=[
            Hypothesis(hid="H1", statement="提价能力延续",
                       indicators=["茅台酒批价 (年报渠道数据)"]),
            Hypothesis(hid="H2", statement="直销占比提升",
                       indicators=["直销渠道收入占比 (fin.income 主营构成)"]),
        ])


# ---------- 满分样例 ----------

def test_full_score():
    draft, facts = full_draft(), mk_facts()
    res = qr.score(draft, facts, VIEWS)
    assert res["score"] == 100.0
    assert res["pass"] is True
    assert all(d["pass"] for d in res["dimensions"].values())
    # 满分草稿对 T01 校验器也应零错误 (交叉一致性)
    assert validate_draft(draft, facts, VIEWS) == []


def test_weights_sum_to_100():
    assert sum(d["weight"] for d in qr.score(
        full_draft(), mk_facts(), VIEWS)["dimensions"].values()) == 100


# ---------- 踩线样例 (每维恰好压达标线, 整体仍通过) ----------

def test_boundary_all_lines():
    # 覆盖率恰好 80%: 全稿数字句 50 (被引 40 / 未引 10); 第1章的 2 句无归属观点句
    # 也在"被引"内 (≤2 达标); 证据恰好 100 条 / 年报 15 条
    chapters = []
    for no in range(1, 11):
        cited_e = [f"营收为{100 + i}亿元 [E:T:tushare.metric{i}.2021A]"
                   for i in range(4)]
        if no == 1:
            sents = cited_e[:2] + [
                "需求将增长15% [V:R:101.1]",        # 无机构标注 ×2 (≤2 达标)
                "出货量将提升20% [V:R:101.1]",
                "总资产为2000亿元但未引用",
            ]
            used = [f"T:tushare.metric{i}.2021A" for i in range(2)] + ["R:101.1"]
        else:
            sents = cited_e + ["总资产为2000亿元但未引用"]
            used = [f"T:tushare.metric{i}.2021A" for i in range(4)]
        chapters.append(mk_chapter(no, sents, used_eids=used))
    draft = Draft(ts_code="600519.SH", name="贵州茅台", chapters=chapters,
                  hypotheses=[Hypothesis(hid="H1", statement="s",
                                         indicators=["指标x"])])
    facts = mk_facts(total=100, annual=15)   # 恰好压 ≥100 / ≥15
    res = qr.score(draft, facts, VIEWS)

    assert res["pass"] is True
    assert res["dimensions"]["evidence_coverage"]["pass"] is True
    assert res["dimensions"]["evidence_coverage"]["detail"]["numeric_sentences"] == 50
    assert res["dimensions"]["evidence_coverage"]["detail"]["cited_sentences"] == 40
    assert res["dimensions"]["layer_purity"]["pass"] is True
    assert res["dimensions"]["layer_purity"]["detail"]["unattributed_view_sentences"] == 2
    assert res["dimensions"]["evidence_richness"]["pass"] is True
    assert res["dimensions"]["evidence_richness"]["detail"]["fact_count"] == 100
    assert res["dimensions"]["evidence_richness"]["detail"]["annual_fact_count"] == 15
    # 总分: 30*0.8 + 20 + 15 + 15 + 10 + 10*0.6 = 90
    assert res["score"] == pytest.approx(90.0)


# ---------- 单维失败样例 ----------

def _base_res(**kw):
    """满分基底上做单点破坏的便捷入口"""
    return qr.score(kw.pop("draft", full_draft()),
                    kw.pop("facts", mk_facts()),
                    kw.pop("views", VIEWS), **kw)


def test_fail_coverage():
    # 每章 5 个数字句仅 1 个被引 → 全稿覆盖率 10/50 = 20%
    chapters = [mk_chapter(no, [
        "营收为100亿元 [E:T:tushare.metric0.2021A]",
        "净利为50亿元但未引用",
        "毛利率为91%但未引用",
        "总资产为2000亿元但未引用",
        "现金流为300亿元但未引用",
    ], used_eids=["T:tushare.metric0.2021A"]) for no in range(1, 11)]
    draft = Draft(ts_code="600519.SH", name="贵州茅台", chapters=chapters,
                  hypotheses=[Hypothesis(hid="H1", statement="s",
                                         indicators=["指标x"])])
    res = qr.score(draft, mk_facts(), VIEWS)
    assert res["dimensions"]["evidence_coverage"]["pass"] is False
    assert res["dimensions"]["evidence_coverage"]["ratio"] == pytest.approx(0.2)
    assert res["pass"] is False


def test_fail_ghost_in_body():
    ch = mk_chapter(1, ["营收为100亿元 [E:T:ghost.2021A]"], used_eids=["T:ghost.2021A"])
    draft = full_draft()
    draft.chapters[0] = ch
    res = qr.score(draft, mk_facts(), VIEWS)
    assert res["dimensions"]["citation_validity"]["pass"] is False
    assert res["dimensions"]["citation_validity"]["detail"]["ghosts"] == ["T:ghost.2021A"]
    assert res["dimensions"]["citation_validity"]["weighted"] == 0.0


def test_fail_ghost_bad_prefix_in_used_eids():
    ch = full_chapter(1)
    ch.used_eids.append("X:非法前缀")
    draft = full_draft()
    draft.chapters[0] = ch
    res = qr.score(draft, mk_facts(), VIEWS)
    assert "X:非法前缀" in res["dimensions"]["citation_validity"]["detail"]["ghosts"]


def test_fail_ghost_from_flags_union():
    res = _base_res(flags=["第3章 行内引用 [V:R:bad.9] 不在证据集"])
    ghosts = res["dimensions"]["citation_validity"]["detail"]["ghosts"]
    assert "R:bad.9" in ghosts
    assert res["dimensions"]["citation_validity"]["pass"] is False


def test_fail_structure_missing_chapters():
    draft = full_draft()
    draft.chapters = draft.chapters[:8]   # 缺第 9、10 章
    res = qr.score(draft, mk_facts(), VIEWS)
    d = res["dimensions"]["structure"]
    assert d["pass"] is False
    assert d["detail"]["missing"] == [9, 10]


def test_structure_skip_flag_exempts():
    draft = full_draft()
    draft.chapters = draft.chapters[:9]   # 缺第 10 章
    res = qr.score(draft, mk_facts(), VIEWS,
                   flags=["第10章 因证据不足跳过"])
    d = res["dimensions"]["structure"]
    assert d["pass"] is True
    assert d["detail"]["skipped_by_flags"] == [10]


def test_fail_richness_facts_short():
    res = _base_res(facts=mk_facts(total=99, annual=16))
    d = res["dimensions"]["evidence_richness"]
    assert d["pass"] is False
    assert d["detail"]["fact_count"] == 99


def test_fail_richness_annual_short():
    res = _base_res(facts=mk_facts(total=105, annual=14))
    d = res["dimensions"]["evidence_richness"]
    assert d["pass"] is False
    assert d["detail"]["annual_fact_count"] == 14


def test_fail_hypothesis_without_indicators():
    draft = full_draft()
    draft.hypotheses.append(Hypothesis(hid="H3", statement="无指标假设"))
    res = qr.score(draft, mk_facts(), VIEWS)
    d = res["dimensions"]["hypothesis"]
    assert d["pass"] is False
    assert d["detail"]["observed"] == "含指标假设 2/3"


def test_fail_no_hypotheses():
    draft = full_draft()
    draft.hypotheses = []
    d = qr.score(draft, mk_facts(), VIEWS)["dimensions"]["hypothesis"]
    assert d["pass"] is False
    assert d["ratio"] == 0.0
    assert d["detail"]["observed"] == "无假设 (判断层缺失)"


def test_fail_purity_over_line():
    chapters = []
    for no in range(1, 11):
        sents = [f"营收为{100 + no}亿元 [E:T:tushare.metric{no}.2021A]"]
        if no <= 3:   # 3 句无归属观点句 > 达标线 2
            sents.append(f"需求将增长{no}0% [V:R:101.1]")
        chapters.append(mk_chapter(no, sents,
                                   used_eids=[f"T:tushare.metric{no}.2021A", "R:101.1"]))
    draft = Draft(ts_code="600519.SH", name="贵州茅台", chapters=chapters,
                  hypotheses=[Hypothesis(hid="H1", statement="s",
                                         indicators=["指标x"])])
    d = qr.score(draft, mk_facts(), VIEWS)["dimensions"]["layer_purity"]
    assert d["pass"] is False
    assert d["detail"]["unattributed_view_sentences"] == 3


# ---------- 口径细节 ----------

def test_iter_sentences_excludes_non_prose():
    body = ("## 二、行业分析\n"
            "| 指标 | 2024 |\n|---|---|\n| 营收 | 1741 |\n"
            "```python\nx = 1\n```\n"
            "正文第一句。正文第二句；分号句\n"
            "- 列表项一句")
    sents = list(qr._iter_sentences(body))
    assert sents == ["正文第一句", "正文第二句", "分号句", "列表项一句"]


def test_numeric_detection_strips_markers_and_xref():
    assert not qr._is_numeric_sentence("[E:T:tushare.metric0.2021A] 持续增长")  # 记号内数字不计
    assert not qr._is_numeric_sentence("经营情况见表2")           # 交叉引用非数据主张
    assert not qr._is_numeric_sentence("详见第3章")
    assert qr._is_numeric_sentence("营收增长15%")
    assert qr._is_numeric_sentence("增长１５个百分点")            # 全角数字


def test_coverage_vacuous_when_no_numeric_sentences():
    draft = full_draft()
    for ch in draft.chapters:
        ch.body_md = "只有定性描述没有数字"
    res = qr.score(draft, mk_facts(), VIEWS)
    d = res["dimensions"]["evidence_coverage"]
    assert d["pass"] is True
    assert d["detail"]["numeric_sentences"] == 0


def test_org_cores_strip_suffixes():
    cores = qr._org_cores("国泰君安证券")
    assert "国泰君安" in cores
    assert all(len(c) >= 2 for c in cores)


def test_attribution_by_org_core_and_by_generic():
    sent1 = "国泰君安预计高端酒动销回暖 [V:R:102.1]"
    assert qr._view_attributed(sent1, ["R:102.1"], VIEW_BY_VID) is True
    sent2 = "多家券商认为行业景气延续 [V:R:101.1]"     # 泛指归属词
    assert qr._view_attributed(sent2, ["R:101.1"], VIEW_BY_VID) is True
    sent3 = "行业景气将延续 [V:R:101.1]"                # 机构与归属词皆无
    assert qr._view_attributed(sent3, ["R:101.1"], VIEW_BY_VID) is False


def test_ghost_vid_sentence_not_double_penalized_as_impure():
    """幽灵 [V:] 引用只计入 citation_validity, 不再进纯净度分母"""
    ch = mk_chapter(1, ["行业景气将延续 [V:R:bad.9]"], used_eids=["R:bad.9"])
    draft = full_draft()
    draft.chapters[0] = ch
    res = qr.score(draft, mk_facts(), VIEWS)
    assert res["dimensions"]["layer_purity"]["detail"]["unattributed_view_sentences"] == 0
    assert "R:bad.9" in res["dimensions"]["citation_validity"]["detail"]["ghosts"]


# ---------- 目录级入口与报告 ----------

def test_score_from_dir_roundtrip(tmp_path: Path):
    from skills.research.contracts import EvidenceShard

    rdir = tmp_path / "research" / "600519.SH"
    draft, facts = full_draft(), mk_facts()
    save_shard(EvidenceShard(kind="tushare", ts_code="600519.SH",
                             facts=[f for f in facts if f.eid.startswith("T:")]),
               rdir / "facts_tushare.json")
    save_shard(EvidenceShard(kind="annual", ts_code="600519.SH",
                             facts=[f for f in facts if f.eid.startswith("A:")]),
               rdir / "facts_annual_2025.json")
    save_shard(EvidenceShard(kind="reports", ts_code="600519.SH", views=VIEWS),
               rdir / "views_reports.json")
    save_draft(draft, rdir / "draft.json")

    res_dir = qr.score_from_dir(rdir)
    res_mem = qr.score(draft, facts, VIEWS)
    assert res_dir == res_mem
    assert res_dir["pass"] is True


def test_score_from_dir_missing_draft(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        qr.score_from_dir(tmp_path)


def test_score_deterministic():
    r1 = qr.score(full_draft(), mk_facts(), VIEWS)
    r2 = qr.score(full_draft(), mk_facts(), VIEWS)
    assert r1 == r2


def test_format_report_renders_all_dimensions():
    res = qr.score(full_draft(), mk_facts(), VIEWS)
    report = qr.format_report(res)
    assert "总分 100.0 / 100" in report
    for d in res["dimensions"].values():
        assert d["label"] in report
    assert "✅" in report
