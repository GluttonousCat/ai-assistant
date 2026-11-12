# -*- encoding: utf-8 -*-
"""T01 contract validator tests — fully offline (no LLM/DB), covering every validation rule.

Groups:
  test_valid_*    legal fixtures (tushare/annual/reports shards + a legal draft)
  test_invalid_*  illegal fixtures (one trigger per rule, asserting errors localize to the exact entry)
  test_roundtrip  existing-API compatibility: save/load shards and drafts, load_pack merge dedup
  test_stats      pack_stats source/period distribution stats
"""
from pathlib import Path

import pytest

from skills.research.contracts import (
    Chapter,
    Draft,
    EvidenceShard,
    Fact,
    Hypothesis,
    View,
    load_draft,
    load_pack,
    load_shard,
    pack_stats,
    save_draft,
    save_shard,
    valid_period,
    validate_draft,
    validate_shard,
)


# ---------- fixture builders ----------

def _fact(**kw) -> Fact:
    base = dict(
        eid="T:tushare.revenue.2025A",
        content="2025 年度营业收入 1741.44 亿元",
        source="fin.income.revenue",
        period="2025A",
        value=1741.44,
        unit="亿元",
    )
    base.update(kw)
    return Fact(**base)


def _view(**kw) -> View:
    base = dict(
        vid="R:1001.1",
        statement="预计 2026 年茅台酒销量同比增长 5%。",
        org="中信证券",
        publish_date="2026-03-28",
        report_id=1001,
        period="2026A",
        category="forecast",
    )
    base.update(kw)
    return View(**base)


def _shard_tushare() -> EvidenceShard:
    return EvidenceShard(
        kind="tushare",
        ts_code="600519.SH",
        facts=[
            _fact(eid="T:tushare.revenue.2025A", period="2025A"),
            _fact(eid="T:tushare.revenue.2025Q3", content="2025 三季报营业收入 1231 亿",
                  period="2025Q3", value=1231.0),
            _fact(eid="T:tushare.close.2025-12-31",
                  content="2025-12-31 收盘价 1480 元", period="2025-12-31",
                  value=1480.0, unit="元"),
            _fact(eid="T:tushare.pe_band.2025H1",
                  content="2025 上半年市盈率处于历史中位", period="2025H1",
                  value=None, unit="倍"),
            # purely qualitative fact: content has no digits, value/unit not enforced
            _fact(eid="T:tushare.profile.2025", content="公司为国内白酒行业龙头",
                  period="2025", value=None, unit=""),
        ],
    )


def _shard_annual() -> EvidenceShard:
    return EvidenceShard(
        kind="annual",
        ts_code="600519.SH",
        facts=[
            _fact(eid="A:2025.mdna.sub3", content="年报经营情况分析提到销量 8.2 万吨",
                  source="2025年报·管理层讨论·三、经营情况", period="2025A",
                  value=8.2, unit="万吨"),
            _fact(eid="A:2025.shareholders.p1", content="前十大股东合计持股 61%",
                  source="2025年报·股东情况", period="2025A", value=61.0, unit="%"),
        ],
    )


def _shard_reports() -> EvidenceShard:
    return EvidenceShard(
        kind="reports",
        ts_code="600519.SH",
        views=[
            _view(vid="R:1001.1"),
            _view(vid="R:1002.1", statement="给予买入评级。", org="国泰君安",
                  publish_date="2026-04-02", category="rating", period=""),
            _view(vid="R:1002.2", statement="提价落地节奏是核心风险。",
                  org="国泰君安", publish_date="2026-04-02", category="risk",
                  period="2026"),
        ],
    )


def _facts_and_views():
    s1, s2, s3 = _shard_tushare(), _shard_annual(), _shard_reports()
    return ([*s1.facts, *s2.facts], [*s3.views])


def _draft() -> Draft:
    facts, views = _facts_and_views()
    return Draft(
        ts_code="600519.SH",
        name="贵州茅台",
        chapters=[
            Chapter(no=1, title="摘要",
                    body_md="公司收入稳定增长 [E:T:tushare.revenue.2025A]。",
                    used_eids=["T:tushare.revenue.2025A"]),
            Chapter(no=7, title="估值与观点",
                    body_md="头部机构普遍乐观 [V:R:1001.1] [V:R:1002.1]。",
                    used_eids=["R:1001.1", "R:1002.1"]),
        ],
        hypotheses=[
            Hypothesis(hid="H1", statement="2026 年批价企稳回升",
                       indicators=["飞天茅台批价月度环比 (渠道调研)"],
                       related_eids=["A:2025.mdna.sub3"]),
        ],
    )


def _errs_of(shard_or_draft, *args):
    """Run validation and return the error list (validate_shard takes 1 arg / validate_draft 2)"""
    if args:
        return validate_draft(shard_or_draft, *args)
    return validate_shard(shard_or_draft)


def _has(errors, key: str) -> bool:
    return any(key in e for e in errors)


# ---------- validate_shard: legal fixtures ----------

@pytest.mark.parametrize("shard", [_shard_tushare(), _shard_annual(), _shard_reports()],
                         ids=["tushare", "annual", "reports"])
def test_valid_shards(shard):
    assert validate_shard(shard) == []


def test_valid_period_formats():
    for p in ["2025", "2025A", "2026E", "2025H1", "2025Q1", "2025Q4", "2025-12-31", "1999-01-01"]:
        assert valid_period(p), p


# ---------- validate_shard: illegal fixtures (one case per rule) ----------

def test_invalid_duplicate_eid():
    shard = _shard_tushare()
    dup = _fact(eid="T:tushare.revenue.2025A", content="重复条目 1 亿元")
    shard.facts.append(dup)
    errors = validate_shard(shard)
    assert _has(errors, "eid 重复") and _has(errors, "T:tushare.revenue.2025A")


def test_invalid_duplicate_vid():
    shard = _shard_reports()
    shard.views.append(_view(vid="R:1001.1", statement="重复观点"))
    errors = validate_shard(shard)
    assert _has(errors, "vid 重复") and _has(errors, "R:1001.1")


def test_invalid_eid_prefix():
    shard = _shard_tushare()
    shard.facts[0].eid = "X:unknown.revenue.2025A"
    errors = validate_shard(shard)
    assert _has(errors, "eid 前缀非法") and _has(errors, "X:unknown.revenue.2025A")


def test_invalid_vid_prefix():
    shard = _shard_reports()
    shard.views[0].vid = "T:tushare.opinion.1"   # fact prefix sneaking into the view slot
    errors = validate_shard(shard)
    assert _has(errors, "vid 前缀非法") and _has(errors, "T:tushare.opinion.1")


def test_invalid_fact_layer():
    shard = _shard_tushare()
    shard.facts[0].layer = "view"                # view sneaking into the fact layer
    errors = validate_shard(shard)
    assert _has(errors, "layer 必须为 'fact'") and _has(errors, "facts[0]")


def test_invalid_view_org_empty():
    shard = _shard_reports()
    shard.views[0].org = ""
    errors = validate_shard(shard)
    assert _has(errors, "View.org 为空") and _has(errors, "views[0]")


def test_invalid_view_publish_date_empty():
    shard = _shard_reports()
    shard.views[0].publish_date = ""
    errors = validate_shard(shard)
    assert _has(errors, "View.publish_date 为空") and _has(errors, "views[0]")


@pytest.mark.parametrize("period", ["2025年", "FY2025", "2025Q5", "2025-13-01",
                                    "2025-02-30", "2025-1-1", "25A", "2026EE", ""])
def test_invalid_period_format(period):
    shard = _shard_tushare()
    shard.facts[0].period = period
    errors = validate_shard(shard)
    if period:   # empty string means "no period"; format not validated
        assert _has(errors, "period 格式非法"), period


def test_invalid_period_empty_is_allowed():
    shard = _shard_tushare()
    shard.facts[0].period = ""
    assert validate_shard(shard) == []


def test_invalid_content_digit_without_value_unit():
    shard = _shard_tushare()
    shard.facts[0].content = "2025 年营收约 1741 亿元"
    shard.facts[0].value = None
    shard.facts[0].unit = ""
    errors = validate_shard(shard)
    assert _has(errors, "value 与 unit 均空") and _has(errors, "T:tushare.revenue.2025A")


def test_content_without_digit_needs_no_value_unit():
    # purely qualitative statement (content without digits): value/unit not enforced
    shard = _shard_tushare()
    shard.facts[0].content = "公司为国内白酒行业龙头"
    shard.facts[0].value = None
    shard.facts[0].unit = ""
    assert validate_shard(shard) == []

# ---------- validate_draft ----------

def test_valid_draft():
    facts, views = _facts_and_views()
    assert validate_draft(_draft(), facts, views) == []


def test_invalid_draft_used_eid_missing():
    facts, views = _facts_and_views()
    draft = _draft()
    draft.chapters[0].used_eids.append("T:tushare.revenue.2099A")
    errors = validate_draft(draft, facts, views)
    assert _has(errors, "引用不存在的事实") and _has(errors, "T:tushare.revenue.2099A")


def test_invalid_draft_used_vid_missing():
    facts, views = _facts_and_views()
    draft = _draft()
    draft.chapters[1].used_eids.append("R:9999.9")
    errors = validate_draft(draft, facts, views)
    assert _has(errors, "引用不存在的观点") and _has(errors, "R:9999.9")


def test_invalid_draft_used_bad_prefix():
    facts, views = _facts_and_views()
    draft = _draft()
    draft.chapters[0].used_eids.append("X:oops")
    errors = validate_draft(draft, facts, views)
    assert _has(errors, "前缀非法") and _has(errors, "X:oops")


def test_invalid_draft_inline_citation_missing():
    facts, views = _facts_and_views()
    draft = _draft()
    draft.chapters[1].body_md += " 另有机构提示风险 [V:R:8888.1]。"
    errors = validate_draft(draft, facts, views)
    assert _has(errors, "[V:R:8888.1]") and _has(errors, "不在证据集")


def test_invalid_draft_inline_fact_citation_missing():
    facts, views = _facts_and_views()
    draft = _draft()
    draft.chapters[0].body_md = "毛利率为 91% [E:T:tushare.gpm.2025A]。"
    errors = validate_draft(draft, facts, views)
    assert _has(errors, "不在证据集") and _has(errors, "T:tushare.gpm.2025A")


def test_invalid_draft_hypothesis_without_indicator():
    facts, views = _facts_and_views()
    draft = _draft()
    draft.hypotheses.append(Hypothesis(hid="H2", statement="无指标假设"))
    errors = validate_draft(draft, facts, views)
    assert _has(errors, "缺验证指标") and _has(errors, "H2")


def test_invalid_draft_hypothesis_blank_indicator():
    facts, views = _facts_and_views()
    draft = _draft()
    draft.hypotheses.append(Hypothesis(hid="H3", statement="空白指标假设",
                                       indicators=["", "   "]))
    errors = validate_draft(draft, facts, views)
    assert _has(errors, "缺验证指标") and _has(errors, "H3")


def test_valid_draft_mixed_refs_across_pools():
    # R:/A: references may mix in any chapter, each validated against the view/fact pool
    # (existence is all that matters)
    facts, views = _facts_and_views()
    draft = _draft()
    draft.chapters[0].used_eids.append("R:1001.1")
    draft.chapters[0].used_eids.append("A:2025.mdna.sub3")
    assert validate_draft(draft, facts, views) == []


# ---------- existing-API compatibility: serialization round trip ----------

def test_shard_roundtrip(tmp_path: Path):
    shard = _shard_tushare()
    p = tmp_path / "facts_tushare.json"
    save_shard(shard, p)
    assert load_shard(p) == shard


def test_draft_roundtrip(tmp_path: Path):
    draft = _draft()
    p = tmp_path / "draft.json"
    save_draft(draft, p)
    assert load_draft(p) == draft


def test_load_pack_merges_and_dedupes(tmp_path: Path):
    save_shard(_shard_annual(), tmp_path / "facts_annual_2025.json")
    save_shard(_shard_reports(), tmp_path / "views_reports.json")
    # inject into the tushare shard an eid duplicating one in annual: glob order loads
    # the annual file first; first-wins keeps the annual copy, the tushare dup is ignored
    tushare = _shard_tushare()
    tushare.facts.append(_fact(eid="A:2025.mdna.sub3", content="撞车条目"))
    save_shard(tushare, tmp_path / "facts_tushare.json")
    # non-shard json must be skipped
    (tmp_path / "notes.json").write_text('{"foo": 1}', encoding="utf-8")
    # draft/report filename prefixes must be skipped
    save_draft(_draft(), tmp_path / "draft.json")

    facts, views = load_pack(tmp_path)
    assert len(facts) == 5 + 2                 # tushare 5 + annual 2 (the dup ignored)
    assert len(views) == 3
    assert not any("撞车" in f.content for f in facts)
    assert {f.eid for f in facts} == {
        "T:tushare.revenue.2025A", "T:tushare.revenue.2025Q3",
        "T:tushare.close.2025-12-31", "T:tushare.pe_band.2025H1",
        "T:tushare.profile.2025", "A:2025.mdna.sub3", "A:2025.shareholders.p1",
    }


def test_validate_shard_after_roundtrip(tmp_path: Path):
    # the validator also applies to shards saved-then-loaded (dict hydration loses no fields)
    save_shard(_shard_reports(), tmp_path / "views_reports.json")
    facts, views = load_pack(tmp_path)
    rebuilt = EvidenceShard(kind="reports", ts_code="600519.SH", views=views)
    assert validate_shard(rebuilt) == []


# ---------- pack_stats ----------

def test_pack_stats():
    facts, views = _facts_and_views()
    stats = pack_stats(facts, views)
    assert stats["fact_count"] == 7
    assert stats["view_count"] == 3
    assert stats["numeric_fact_count"] == 5        # two qualitative ones: pe_band / profile
    assert stats["facts_by_kind"] == {"tushare": 5, "annual": 2}
    assert stats["views_by_kind"] == {"reports": 3}
    # org distribution: count desc (国泰君安 with 2 first)
    assert stats["views_by_org"] == {"国泰君安": 2, "中信证券": 1}
    # period distribution: 2025A x3 (tushare revenue + annual's two), others 1 each
    assert stats["facts_by_period"]["2025A"] == 3
    assert stats["facts_by_period"]["2025-12-31"] == 1
    assert stats["views_by_period"] == {"2026": 1, "2026A": 1, "unknown": 1}
    # period keys ordered, for direct comparison in the scorecard/debugging
    assert list(stats["facts_by_period"]) == sorted(stats["facts_by_period"])


def test_pack_stats_views_by_org_ordering():
    # equal counts break ties by org name asc (中 U+4E2D < 国 U+56FD); empty org -> unknown
    views = [
        _view(vid="R:2001.1", org="国泰君安"),
        _view(vid="R:2002.1", org="中信证券"),
        _view(vid="R:2003.1", org="中信证券"),
        _view(vid="R:2004.1", org="国泰君安"),
        _view(vid="R:2005.1", org=""),
    ]
    stats = pack_stats([], views)
    assert list(stats["views_by_org"]) == ["中信证券", "国泰君安", "unknown"]
    assert stats["views_by_org"] == {"中信证券": 2, "国泰君安": 2, "unknown": 1}


def test_pack_stats_empty_and_unknown_prefix():
    stats = pack_stats([], [])
    assert stats["fact_count"] == 0 and stats["facts_by_kind"] == {}
    assert stats["views_by_org"] == {}
    stats2 = pack_stats([_fact(eid="X:bad.prefix", period="")], [])
    assert stats2["facts_by_kind"] == {"other": 1}
    assert stats2["facts_by_period"] == {"unknown": 1}
