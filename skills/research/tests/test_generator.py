# -*- encoding: utf-8 -*-
"""T06 草稿生成器测试 — 全部离线: 注入 FakeLLM, 不连真实模型/DB。

分组:
  test_prompt_lib    提示词库加载 (T05 未合并时回退 stub, 10 章契约仍成立)
  test_select        wants 证据路由与未命中回退
  test_digest        摘要化: 条数上限/期间排序/字符截断
  test_resolve       股票名/代码 → 目录解析
  test_parse_hyp     假设 JSON 宽容解析 (围栏/裸数组)
  test_generate      端到端: 十章生成/失败跳过/重试/years 过滤/flags 落盘
"""
import json
import re
from pathlib import Path

import pytest

from skills.research.compose import generator as gen
from skills.research.contracts import (
    Draft,
    EvidenceShard,
    Fact,
    View,
    load_draft,
    save_shard,
)

# ---------- fixtures ----------

PROFILE_FACTS = [
    Fact(eid="T:profile.mainbiz.2025A", content="2025年茅台酒收入1459亿元, 占比83.9%",
         source="profile.main", period="2025A", value=1459.0, unit="亿元"),
    Fact(eid="T:profile.industry.2025A", content="所属行业为白酒",
         source="profile.industry", period="2025A"),
]
TUSHARE_FACTS = [
    Fact(eid="T:tushare.revenue.2025A", content="2025年营业收入1741亿元",
         source="fin.income.revenue", period="2025A", value=1741.0, unit="亿元"),
    Fact(eid="T:tushare.roe.2022A", content="2022年ROE为31.7%",
         source="fin.indicator.roe", period="2022A", value=31.7, unit="%"),
    Fact(eid="T:tushare.roe.2019A", content="2019年ROE为33.1%",        # 回看窗口外, 应被过滤
         source="fin.indicator.roe", period="2019A", value=33.1, unit="%"),
]
ANNUAL_FACTS = [
    Fact(eid="A:2025.mdna.1", content="2025年茅台酒基酒产量约5.6万吨",
         source="2025年报·mdna·三、经营情况", period="2025A", value=5.6, unit="万吨"),
]
VIEWS = [
    View(vid="R:101.1", statement="预计2026年营收增长10%", org="中金",
         publish_date="2026-03-01", period="2026A"),
    View(vid="R:102.1", statement="产能扩张节奏可控", org="国信",
         publish_date="2026-02-14", period="2026A"),
]
ALL_FACTS = PROFILE_FACTS + TUSHARE_FACTS + ANNUAL_FACTS

CH10_JSON = [{
    "statement": "2026年营收增速不低于8%",
    "indicators": ["季度营收增速 (Tushare 利润表)"],
    "related_eids": ["T:tushare.revenue.2025A"],
}]


def _make_pack(d: Path, ts_code: str = "600519.SH", name: str = "贵州茅台") -> Path:
    """写三源分片到 d (模拟 Stage 1 产物)。"""
    save_shard(EvidenceShard(kind="tushare", ts_code=ts_code,
                             facts=PROFILE_FACTS + TUSHARE_FACTS,
                             meta={"name": name}), d / "facts_tushare.json")
    save_shard(EvidenceShard(kind="annual", ts_code=ts_code, facts=ANNUAL_FACTS,
                             meta={"name": name}), d / "facts_annual_2025.json")
    save_shard(EvidenceShard(kind="reports", ts_code=ts_code, views=VIEWS,
                             meta={"name": name}), d / "views_reports.json")
    return d


class FakeLLM:
    """按章返回预置正文; 可注入失败章/前 N 次调用失败, 并记录 prompt 供断言。"""

    def __init__(self, fail_nos=(), fail_first=0, hyp_with_json=True):
        self.fail_nos = set(fail_nos)
        self.fail_first = fail_first
        self.calls = 0
        self.prompts: list[str] = []
        self.hyp_with_json = hyp_with_json

    def _body(self, no: int) -> str:
        body = (f"### 第{no}章小节\n"
                "2025年营业收入1741亿元[E:T:tushare.revenue.2025A]。"
                "中金(2026-03-01)认为高端白酒需求稳健[V:R:101.1]。"
                "净利润达862亿元。"                     # 无引用数字 → 应出 unverified-number
                "本报告覆盖2021-2025年经营历史。")       # 纯年份 → 不应告警
        if no == 8 and self.hyp_with_json:
            body += "\n```json\n" + json.dumps(CH10_JSON, ensure_ascii=False) + "\n```"
        return body

    def invoke(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls <= self.fail_first:
            raise RuntimeError("模拟首次调用失败")
        m = re.search(r"第(\d+)章", prompt)
        no = int(m.group(1)) if m else 0
        if no in self.fail_nos:
            raise RuntimeError(f"模拟第{no}章失败")
        return self._body(no)


# ---------- 提示词库 ----------

def test_prompt_lib_has_ten_chapters():
    chapters, style, fn, hyp_no = gen._load_prompt_lib()
    assert 1 <= hyp_no <= 10
    assert len(chapters) == 10
    for ch in chapters:
        assert ch["title"] and ch["question"] and ch["wants"] and ch["structure"]
        assert int(ch["no"]) in range(1, 11)
    assert "三层分离" in style and "[E:eid]" in style and "待验证" in style
    out = fn(chapters[0], "事实摘要X", "观点摘要Y")
    assert "事实摘要X" in out and "观点摘要Y" in out and chapters[0]["question"] in out


def test_prompts_stub_is_usable_fallback():
    """T05 未合并时 (本分支独立检出) 的回退保障: stub 自身满足 10 章契约。"""
    from skills.research.compose import _prompts_stub as stub
    assert len(stub.CHAPTERS) == 10
    assert stub.HYPOTHESES_CHAPTER_NO == 8
    assert "三层分离" in stub.STYLE_PROMPT
    out = stub.chapter_prompt(stub.CHAPTERS[0], "F", "V")
    assert "F" in out and "V" in out
    assert "研究假设" in stub.chapter_prompt(stub.CHAPTERS[7], "F", "V")   # 假设章注入 JSON 要求


# ---------- 证据路由 ----------

def test_select_for_want_categories():
    assert [f.eid for f in gen._select_for_want("profile", ALL_FACTS, VIEWS)[0]] == \
        ["T:profile.mainbiz.2025A", "T:profile.industry.2025A"]
    assert [f.eid for f in gen._select_for_want("mainbiz", ALL_FACTS, VIEWS)[0]] == \
        ["T:profile.mainbiz.2025A"]                       # 指标 token 命中
    assert [f.eid for f in gen._select_for_want("annual", ALL_FACTS, VIEWS)[0]] == \
        ["A:2025.mdna.1"]
    assert [f.eid for f in gen._select_for_want("tushare", ALL_FACTS, VIEWS)[0]] == \
        ["T:tushare.revenue.2025A", "T:tushare.roe.2022A", "T:tushare.roe.2019A"]
    assert gen._select_for_want("views", ALL_FACTS, VIEWS)[1] == VIEWS
    assert gen._select_for_want("reports", ALL_FACTS, VIEWS)[1] == VIEWS


def test_select_chapter_evidence_dedup_and_fallback():
    sf, sv, flags = gen.select_chapter_evidence(["profile", "views", "profile"],
                                                ALL_FACTS, VIEWS)
    assert [f.eid for f in sf] == ["T:profile.mainbiz.2025A", "T:profile.industry.2025A"]
    assert [v.vid for v in sv] == ["R:101.1", "R:102.1"]
    assert flags == []
    sf2, sv2, flags2 = gen.select_chapter_evidence(["不存在的类别"], ALL_FACTS, VIEWS)
    assert len(sf2) == len(ALL_FACTS) and len(sv2) == len(VIEWS)
    assert flags2 and flags2[0].startswith("evidence-route-fallback")


# ---------- 摘要化 ----------

def test_build_digest_caps_and_order():
    facts = [Fact(eid=f"T:tushare.roe.{y}A", content=f"{y}年ROE为30%",
                  source="s", period=f"{y}A") for y in (2021, 2025, 2023)]
    ftext, vtext, flags = gen.build_digest(4, facts, VIEWS)
    assert ftext.split("\n")[0].startswith("- [E:T:tushare.roe.2025A]")   # 新期间在前
    assert "R:101.1" in vtext and flags == []
    many = facts + [Fact(eid=f"T:tushare.x.{i}", content=f"指标{i}为1亿元",
                         source="s", period="2025A") for i in range(100)]
    ftext, vtext, flags = gen.build_digest(4, many, VIEWS)
    assert len(ftext.split("\n")) == gen.FACT_ITEM_CAP                       # 事实让位至 60
    assert any(f.startswith("digest-truncated") and "截断至 60" in f for f in flags)
    assert len(vtext.split("\n")) == len(VIEWS)                              # 观点保底不被挤出


def test_build_digest_char_cap():
    long_fact = Fact(eid="T:tushare.big.2025A", content="长" * 40000, source="s", period="2025A")
    ftext, vtext, flags = gen.build_digest(2, [long_fact], [])
    assert len(ftext) <= gen.DIGEST_CHAR_CAP
    assert any(f.startswith("digest-too-long") for f in flags)


# ---------- 股票/目录解析 ----------

def test_resolve_stock(tmp_path):
    root = tmp_path / "out"
    d = _make_pack(root / "research" / "600519.SH")
    ts, name, rd = gen.resolve_stock("600519.SH", root)
    assert (ts, name, rd) == ("600519.SH", "贵州茅台", root / "research" / "600519.SH")
    ts2, name2, rd2 = gen.resolve_stock("贵州茅台", root)
    assert (ts2, rd2) == ("600519.SH", rd)
    _make_pack(root / "research" / "000858.SZ", ts_code="000858.SZ", name="五粮液")
    with pytest.raises(ValueError):
        gen.resolve_stock("洋河股份", root)                  # 多目录且名称未命中
    ts3, _, _ = gen.resolve_stock("五粮液", root)
    assert ts3 == "000858.SZ"


def test_resolve_stock_explicit_dir(tmp_path):
    d = _make_pack(tmp_path / "r")
    ts, name, rd = gen.resolve_stock("贵州茅台", tmp_path, d)
    assert (ts, name, rd) == ("600519.SH", "贵州茅台", d)


# ---------- 假设解析 ----------

def test_parse_hypotheses_fenced_and_bare():
    body, items = gen._parse_hypotheses(
        "正文段。\n```json\n" + json.dumps(CH10_JSON, ensure_ascii=False) + "\n```")
    assert items == CH10_JSON and "```" not in body and "正文段" in body
    body2, items2 = gen._parse_hypotheses(
        "正文段。\n" + json.dumps(CH10_JSON, ensure_ascii=False))
    assert items2 == CH10_JSON and "statement" not in body2


def test_parse_hypotheses_none():
    body, items = gen._parse_hypotheses("只有正文, 没有假设。")
    assert items == [] and body == "只有正文, 没有假设。"


# ---------- 端到端 (FakeLLM) ----------

def test_generate_draft_offline(tmp_path):
    d = _make_pack(tmp_path / "research" / "600519.SH")
    llm = FakeLLM()
    draft = gen.generate_draft("600519.SH", years=5, research_dir=d, llm=llm)
    assert len(draft.chapters) == 10
    assert [c.no for c in draft.chapters] == list(range(1, 11))
    ch1 = draft.chapters[0]
    assert "T:tushare.revenue.2025A" in ch1.used_eids and "R:101.1" in ch1.used_eids
    assert "本报告覆盖2021-2025年经营历史" in ch1.body_md
    assert len(llm.prompts) == 10 and "三层分离" in llm.prompts[0]
    assert llm.prompts[0].count("三层分离") == 1    # T05 chapter_prompt 已内嵌文风, 不得重复拼接
    assert "研究假设" in llm.prompts[7]                      # 第 8 章 (假设章) JSON 要求已随模板注入
    assert all("T:tushare.roe.2019A" not in p for p in llm.prompts)   # 窗口外证据已过滤
    # 假设回填: body 不再含 JSON 围栏
    assert len(draft.hypotheses) == 1
    h = draft.hypotheses[0]
    assert h.hid == "H1" and h.indicators and h.related_eids == ["T:tushare.revenue.2025A"]
    assert "```" not in draft.chapters[7].body_md           # 假设章正文已剥离 JSON 围栏
    # flags 机制: 无引用数字被点名, 合法引用不产生硬伤
    assert any(f.startswith("unverified-number") and "862" in f for f in draft.flags)
    assert not [f for f in draft.flags if f.startswith(("ghost-citation", "contract-invalid"))]
    assert not [f for f in draft.flags if "2021-2025" in f]   # 纯年份句不误报
    # 落盘可回读
    loaded = load_draft(d / "draft.json")
    assert loaded.ts_code == "600519.SH" and loaded.name == "贵州茅台"
    assert len(loaded.chapters) == 10


def test_generate_draft_failed_chapters_skipped(tmp_path):
    d = _make_pack(tmp_path / "research" / "600519.SH")
    draft = gen.generate_draft("600519.SH", research_dir=d, llm=FakeLLM(fail_nos={3, 7}))
    assert [c.no for c in draft.chapters] == [1, 2, 4, 5, 6, 8, 9, 10]
    failed = [f for f in draft.flags if f.startswith("chapter-failed")]
    assert len(failed) == 2 and "第3章" in failed[0] and "第7章" in failed[1]
    assert "模拟第3章失败" in failed[0]


def test_generate_draft_all_failed(tmp_path):
    d = _make_pack(tmp_path / "research" / "600519.SH")
    draft = gen.generate_draft("600519.SH", research_dir=d,
                               llm=FakeLLM(fail_nos=set(range(1, 11))))
    assert draft.chapters == [] and draft.hypotheses == []
    assert len([f for f in draft.flags if f.startswith("chapter-failed")]) == 10


def test_generate_draft_retry_recovers(tmp_path):
    d = _make_pack(tmp_path / "research" / "600519.SH")
    llm = FakeLLM(fail_first=1)                              # 仅第 1 次调用失败
    draft = gen.generate_draft("600519.SH", research_dir=d, llm=llm)
    assert len(draft.chapters) == 10 and llm.calls == 11      # 第 1 章重试 1 次成功
    assert not [f for f in draft.flags if f.startswith("chapter-failed")]


def test_generate_draft_hyp_chapter_without_json(tmp_path):
    d = _make_pack(tmp_path / "research" / "600519.SH")
    draft = gen.generate_draft("600519.SH", research_dir=d, llm=FakeLLM(hyp_with_json=False))
    assert len(draft.chapters) == 10 and draft.hypotheses == []
    assert any(f.startswith("hypotheses-missing") for f in draft.flags)


def test_generate_draft_empty_dir_refused(tmp_path):
    empty = tmp_path / "research" / "600519.SH"
    empty.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        gen.generate_draft("600519.SH", research_dir=empty, llm=FakeLLM())
