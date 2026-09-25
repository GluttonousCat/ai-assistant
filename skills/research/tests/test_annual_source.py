# -*- encoding: utf-8 -*-
"""T03 年报细粒度证据抽取测试 — 纯离线 (不连 LLM/DB, FakeLLM 注入), 覆盖解析/清洗/契约化/抽检。

分组:
  test_parse_*    LLM 输出宽容解析 (围栏/裸数组/包装键/垃圾)
  test_sanitize_* 原子清洗 (数值化/period 归一/单位抢救/丢弃规则)
  test_collect_*  端到端 (fixture items → EvidenceShard 落盘 + 抽检清单; 失败小节跳过)
  test_review_*   抽检清单内容与可复现采样
"""
import json
from pathlib import Path

import pytest

from skills.research.contracts import load_shard, validate_shard
from skills.research.sources import annual_source

YEAR = 2025


class FakeLLM:
    """按序吐出预置响应; 响应为 Exception 时抛出, 模拟小节级失败"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture
def items_path(tmp_path: Path) -> Path:
    data = {
        "ts_code": "600519.SH", "report_year": YEAR, "category": "ndbg", "items": {},
        "sections": {
            "mdna": {"subsections": [
                {"title": "报告期内主要经营情况",
                 "text": "公司实现营业收入168,838,102,514.79元。酒类产量116,123.73吨。",
                 "tables": [{"name": "产销量", "unit": "吨",
                             "header": ["主要产品", "单位", "生产量"],
                             "rows": [["酒类", "吨", "116,123.73"]]}]},
                {"title": "公司关于公司未来发展的讨论与分析",
                 "text": "管理层表示, 公司未来将继续推进十四五技改项目建设, 加快产能储备与产品结构优化, 应对市场需求变化。",
                 "tables": []},
            ]},
            "governance": {"subsections": []},
            "matters": {"subsections": [
                {"title": "违规担保情况", "text": "", "tables": []},
            ]},
            "shareholders": {"subsections": [
                {"title": "股东和实际控制人情况", "text": "",
                 "tables": [{"header": [], "rows": [["报告期末股东总数", "56,000"]]}]},
            ]},
        },
    }
    p = tmp_path / "cninfo" / "md" / "600519.SH" / f"{YEAR}_ndbg_items.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ---------- 宽容解析 ----------

def test_parse_fenced_dict():
    text = '```json\n{"facts": [{"category": "operating", "content": "x"}]}\n```'
    assert annual_source._parse_llm_facts(text) == [{"category": "operating", "content": "x"}]


def test_parse_bare_array_with_prose():
    text = '以下是抽取结果:\n[{"category": "risk", "content": "y"}]\n以上。'
    assert annual_source._parse_llm_facts(text) == [{"category": "risk", "content": "y"}]


def test_parse_data_key_and_no_facts_key():
    assert annual_source._parse_llm_facts('{"data": [{"content": "z"}]}') == [{"content": "z"}]
    assert annual_source._parse_llm_facts('{"summary": "无证据"}') == []


def test_parse_garbage_returns_none():
    assert annual_source._parse_llm_facts("抱歉, 我无法完成。") is None
    assert annual_source._parse_llm_facts("") is None
    assert annual_source._parse_llm_facts("```json\n{broken\n```") is None


# ---------- 清洗 ----------

def test_to_float_variants():
    assert annual_source._to_float("116,123.73") == 116123.73
    assert annual_source._to_float("583.80亿美元") == 583.80
    assert annual_source._to_float(-4.53) == -4.53
    assert annual_source._to_float("不适用") is None
    assert annual_source._to_float(None) is None
    assert annual_source._to_float(True) is None


def test_norm_period_variants():
    assert annual_source._norm_period("2025年", YEAR) == "2025A"
    assert annual_source._norm_period("", YEAR) == "2025A"
    assert annual_source._norm_period("2025年末", YEAR) == "2025-12-31"
    assert annual_source._norm_period("期末", YEAR) == "2025-12-31"
    assert annual_source._norm_period("2024A", YEAR) == "2024A"        # 合法原值保留
    assert annual_source._norm_period("2025-13-01", YEAR) == "2025A"   # 非法日期兜底


def test_sanitize_numeric_without_value_unit_dropped():
    raw = {"category": "operating", "content": "产量为116,123.73",
           "value": None, "unit": "", "period": "2025A"}
    clean, reason = annual_source._sanitize_fact(raw, YEAR)
    assert clean is None and reason == "numeric_without_value_unit"


def test_sanitize_unit_salvaged_from_content():
    raw = {"category": "operating", "content": "国内经销商2,353家",
           "value": None, "unit": "", "period": "2025年末"}
    clean, _ = annual_source._sanitize_fact(raw, YEAR)
    assert clean["unit"] == "家" and clean["period"] == "2025-12-31"


def test_sanitize_unit_must_be_digit_adjacent():
    # "发达国【家】"里的家不是单位 — 无紧邻数字的单位不得抢救 (真实踩坑: 2035年人均GDP条目)
    raw = {"category": "statement",
           "content": "国家十五五规划提出到2035年人均GDP达到中等发达国家水平",
           "value": None, "unit": "", "period": "2025A"}
    clean, reason = annual_source._sanitize_fact(raw, YEAR)
    assert clean is None and reason == "numeric_without_value_unit"


def test_sanitize_bad_category_and_confidence_clamp():
    raw = {"category": "financial", "content": "x", "value": 1, "unit": "元"}
    assert annual_source._sanitize_fact(raw, YEAR)[1] == "bad_category"
    raw2 = {"category": "risk", "content": "政策风险", "confidence": 5.0}
    clean2, _ = annual_source._sanitize_fact(raw2, YEAR)
    assert clean2["confidence"] == 1.0 and clean2["value"] is None


# ---------- 端到端 ----------

_GOOD_SUB1 = """```json
{"facts": [
 {"category": "operating", "content": "贵州茅台2025年度酒类产量116,123.73吨",
  "value": "116,123.73", "unit": "吨", "period": "2025年",
  "quote": "酒类 | 吨 | 116,123.73", "confidence": 1.0, "formula": ""},
 {"category": "segment", "content": "茅台酒2025年度营业收入1,464.99亿元",
  "value": "146,499,906,480.49", "unit": "元", "period": "2025A",
  "quote": "茅台酒 | 146,499,906,480.49", "confidence": 0.9,
  "formula": "亿元=元/1e8"},
 {"category": "financial", "content": "营业成本148.92亿元", "value": 148.92, "unit": "亿元"},
 {"category": "operating", "content": "贵州茅台2025年度酒类产量116,123.73吨",
  "value": 116123.73, "unit": "吨"}
]}
```"""

_BARE_SUB2 = '[{"category": "statement", "content": "管理层表示将持续推进十四五技改项目建设", ' \
              '"value": null, "unit": "", "period": "2025A", "quote": "管理层表示…技改项目建设", ' \
              '"confidence": 1.0, "formula": ""}]'

_TABLE_SUB = """```json
{"facts": [
 {"category": "operating", "content": "报告期末股东总数56,000户",
  "value": null, "unit": "", "period": "2025年末",
  "quote": "报告期末股东总数 | 56,000", "confidence": 1.0, "formula": ""}
]}
```"""


def test_collect_end_to_end(tmp_path: Path, items_path: Path, monkeypatch):
    fake = FakeLLM([_GOOD_SUB1, _BARE_SUB2, _TABLE_SUB])
    monkeypatch.setattr(annual_source, "get_extract_llm", lambda **kw: fake)

    out = tmp_path / "research"
    shard = annual_source.collect_annual_facts(
        "600519.SH", YEAR, out_dir=out, items_path=items_path)

    assert shard.kind == "annual" and shard.ts_code == "600519.SH"
    assert len(fake.calls) == 3                       # 空小节(matters)不发 LLM 调用
    # mdna1: 2条(坏category丢弃1 + 重复去重1) | mdna2: 1 | shareholders: 1
    assert [f.eid for f in shard.facts] == [
        f"A:{YEAR}.mdna.1.1", f"A:{YEAR}.mdna.1.2",
        f"A:{YEAR}.mdna.2.1", f"A:{YEAR}.shareholders.1.1"]
    assert validate_shard(shard) == []                # 契约干净

    by_eid = {f.eid: f for f in shard.facts}
    assert by_eid[f"A:{YEAR}.mdna.1.1"].value == 116123.73
    assert by_eid[f"A:{YEAR}.mdna.1.1"].period == "2025A"          # "2025年"→2025A
    assert "换算" in by_eid[f"A:{YEAR}.mdna.1.2"].source           # confidence<1 注明公式
    assert by_eid[f"A:{YEAR}.shareholders.1.1"].period == "2025-12-31"
    assert by_eid[f"A:{YEAR}.shareholders.1.1"].unit == "户"       # 单位从 content 抢救
    assert "年报·mdna·报告期内主要经营情况" in by_eid[f"A:{YEAR}.mdna.1.1"].source

    assert shard.meta["category_counts"] == {
        "operating": 2, "segment": 1, "statement": 1, "risk": 0}
    assert any(s["reason"] == "empty" and s["title"] == "违规担保情况"
               for s in shard.meta["skipped"])
    assert any(d["reason"] == "bad_category" for d in shard.meta["dropped"])

    # 落盘与回读
    shard_file = out / f"facts_annual_{YEAR}.json"
    assert shard_file.exists()
    reloaded = load_shard(shard_file)
    assert [f.eid for f in reloaded.facts] == [f.eid for f in shard.facts]

    review = out / f"annual_review_{YEAR}.md"
    assert review.exists()
    md = review.read_text(encoding="utf-8")
    assert "抽取句" in md and "原句" in md and "600519.SH" in md


def test_collect_llm_failure_skips_subsection(tmp_path: Path, items_path: Path, monkeypatch):
    fake = FakeLLM([_GOOD_SUB1, RuntimeError("timeout"), _TABLE_SUB])
    monkeypatch.setattr(annual_source, "get_extract_llm", lambda **kw: fake)

    shard = annual_source.collect_annual_facts(
        "600519.SH", YEAR, out_dir=tmp_path / "r2", items_path=items_path)

    assert len(shard.facts) == 3                      # 失败小节的 1 条丢失, 其余保留
    assert any(s["reason"].startswith("llm_error")
               and "未来发展" in s["title"] for s in shard.meta["skipped"])
    assert validate_shard(shard) == []


def test_collect_missing_items_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        annual_source.collect_annual_facts(
            "000000.SZ", YEAR, out_dir=tmp_path, items_path=tmp_path / "nope.json")


# ---------- 抽检清单 ----------

def test_review_md_deterministic_sampling(tmp_path: Path):
    from skills.research.contracts import EvidenceShard, Fact

    shard = EvidenceShard(kind="annual", ts_code="600519.SH")
    shard.meta["fact_categories"] = {}
    for i in range(1, 11):
        eid = f"A:{YEAR}.mdna.1.{i}"
        shard.facts.append(Fact(eid=eid, content=f"事实{i}", source=f"{YEAR}年报·mdna·x",
                                ts_code="600519.SH", period=f"{YEAR}A", value=float(i)))
        shard.meta["fact_categories"][eid] = "operating"

    p1, p2 = tmp_path / "r1.md", tmp_path / "r2.md"
    e1 = annual_source.write_review_md(p1, shard, YEAR, "600519.SH:2025", 1)
    e2 = annual_source.write_review_md(p2, shard, YEAR, "600519.SH:2025", 1)
    assert e1 == e2 and len(e1) == 1                  # 同 seed 采样可复现
    md = p1.read_text(encoding="utf-8")
    assert e1[0] in md and "抽取句" in md and "- [ ] 一致" in md
