# -*- encoding: utf-8 -*-
"""T08 渲染层测试 — 纯离线 (不连 LLM/DB)。

分组:
  test_title/test_footnotes/test_sources  标题区统计 / 脚注编号复用 / 来源区一一对应
  test_ghost/test_flags                   幽灵引用原样保留 / 数据核查提示块
  test_figs                               章末配图插入·相对路径·散图附图区
  test_appendix                           假设表 / 方法论 / 免责声明
  test_cli_*                              总装 CLI --only render 离线链路 (分片+草稿 fixture)
"""
import re
from datetime import date
from pathlib import Path

import pytest

from skills.research.cli import main as cli_main
from skills.research.contracts import Chapter, Draft, EvidenceShard, Fact, Hypothesis, View
from skills.research.contracts import save_draft, save_shard
from skills.research.render.render_md import render_report

_SUP = re.compile(r"<sup>\[(\d+)\]</sup>")
_SRC_LINE = re.compile(r"^- \*\*\[(\d+)\]\*\* (.+)$", re.M)


# ---------- fixture 构造 ----------

def _fact(**kw) -> Fact:
    base = dict(eid="T:tushare.revenue.2025A",
                content="2025 年度营业收入 1741.44 亿元",
                source="fin.income.revenue", period="2025A",
                value=1741.44, unit="亿元")
    base.update(kw)
    return Fact(**base)


def _view(**kw) -> View:
    base = dict(vid="R:1001.1", statement="预计 2026 年茅台酒销量同比增长 5%。",
                org="中信证券", publish_date="2026-03-28", report_id=1001,
                period="2026A", category="forecast")
    base.update(kw)
    return View(**base)


def _draft(**kw) -> Draft:
    base = dict(
        ts_code="600519.SH", name="贵州茅台", generated_at="2026-09-25T10:00:00",
        chapters=[
            Chapter(no=1, title="公司定位与研究结论摘要",
                    body_md="公司 2025 年营收 1741.44 亿元[E:T:tushare.revenue.2025A], "
                            "头部机构看好动销[V:R:1001.1]。"),
            Chapter(no=2, title="经营与财务分析",
                    body_md="基酒产量 [E:A:2025.mdna.sub3.1] 支撑供给判断; "
                            "另有评级观点 [V:R:1002.1], 与前述营收 "
                            "[E:T:tushare.revenue.2025A] 呼应; "
                            "未经证实的说法 [E:T:ghost.2025A] 原样保留。"),
            Chapter(no=3, title="风险与情景", body_md="本章节无引用。"),
        ],
        hypotheses=[
            Hypothesis(hid="H1", statement="2026 年茅台酒销量增速不低于 4%",
                       indicators=["季度批发代理收入增速 (fin.income)",
                                   "i茅台酒平台销量披露"],
                       related_eids=["T:tushare.revenue.2025A"], status="open"),
            Hypothesis(hid="H2", statement="直销占比继续提升|带动毛利率上行",
                       indicators=["直销渠道收入占比 (年报分部数据)"], status="supported"),
        ],
        flags=["unverified-number: 目标价或超一致预期", "ghost-ref: [E:T:ghost.2025A]"],
    )
    base.update(kw)
    d = Draft(**base)
    d.chapters[1].used_eids = ["A:2025.mdna.sub3.1", "R:1002.1",
                               "T:tushare.revenue.2025A"]
    d.chapters[0].used_eids = ["T:tushare.revenue.2025A", "R:1001.1"]
    return d


def _facts():
    return [_fact(),
            _fact(eid="A:2025.mdna.sub3.1", content="2025 年茅台酒基酒产量约 5.63 万吨",
                  source="2025年报·mdna·三、经营情况", value=5.63, unit="万吨",
                  confidence=0.9)]


def _views():
    return [_view(),
            _view(vid="R:1002.1", statement='维持"买入"评级, 目标价 1850 元。',
                  org="招商证券", publish_date="2026-04-02", report_id=1002,
                  category="rating")]


def _render(tmp_path, flags=None, figs=None, draft=None):
    draft = draft or _draft()
    if flags is not None:
        draft.flags = flags
    rdir = tmp_path / "r"
    (rdir / "figs").mkdir(parents=True, exist_ok=True)
    out = rdir / "report_2026-09-25.md"
    render_report(draft, _facts(), _views(),
                  figs if figs is not None else [], out)
    return out.read_text(encoding="utf-8")


def _section(md, prefix):
    for part in re.split(r"\n(?=## )", md):
        if part.startswith("## " + prefix):
            return part
    return ""


# ---------- 标题区 ----------

def test_title_block_stats(tmp_path):
    md = _render(tmp_path)
    assert "# 贵州茅台（600519.SH）研究报告" in md
    assert f"生成日期: {date.today().isoformat()}" in md
    assert "2 条事实, 来自 2 类来源（年报、Tushare）" in md
    assert "2 条观点, 来自 2 家机构" in md


# ---------- 脚注编号 ----------

def test_footnote_numbering_by_first_appearance_and_reuse(tmp_path):
    md = _render(tmp_path)
    ch1, ch2 = _section(md, "一、"), _section(md, "二、")
    # 首次出现序: ch1 → T:revenue=1, R:1001.1=2; ch2 → A:mdna=3, R:1002.1=4
    assert "<sup>[1]</sup>" in ch1 and "<sup>[2]</sup>" in ch1
    assert "<sup>[3]</sup>" not in ch1 and "<sup>[4]</sup>" not in ch1
    assert all(f"<sup>[{n}]</sup>" in ch2 for n in (1, 3, 4))
    # 同一引用全文复用同号, 编号集合恰为 1..4
    assert md.count("<sup>[1]</sup>") == 2
    assert sorted(_SUP.findall(md), key=int) == ["1", "1", "2", "3", "4"]


def test_ghost_ref_kept_literal(tmp_path):
    md = _render(tmp_path)
    assert "[E:T:ghost.2025A]" in _section(md, "二、")     # 不伪装成脚注
    assert max(int(n) for n in _SUP.findall(md)) == 4      # 无幽灵编号


# ---------- 来源区一一对应 (验收核心) ----------

def test_source_area_matches_footnotes(tmp_path):
    md = _render(tmp_path)
    src = _section(md, "证据与观点来源")
    nums = [m.group(1) for m in _SRC_LINE.finditer(src)]
    assert nums == ["1", "2", "3", "4"]                     # 与正文脚注一一对应
    assert not (set(_SUP.findall(md)) - set(nums))          # 无悬空脚注
    lines = {m.group(1): m.group(2) for m in _SRC_LINE.finditer(src)}
    # fact 条目: 陈述 + 来源 + 期间; 推算值标注
    assert "营业收入 1741.44 亿元" in lines["1"] and "来源: fin.income.revenue" in lines["1"]
    assert "期间: 2025A" in lines["1"]
    assert "换算/推算值" in lines["3"] and "2025年报·mdna·三、经营情况" in lines["3"]
    # view 条目: 机构(日期): 主张 + report_id
    assert "中信证券（2026-03-28）" in lines["2"]
    assert "预计 2026 年茅台酒销量同比增长 5%。" in lines["2"] and "研报 #1001" in lines["2"]
    assert "（评级）" in lines["4"] and "招商证券" in lines["4"] and "研报 #1002" in lines["4"]


# ---------- 数据核查提示块 ----------

def test_flags_block(tmp_path):
    md = _render(tmp_path)
    assert "数据核查提示" in md
    assert "unverified-number: 目标价或超一致预期" in md
    assert md.index("数据核查提示") < md.index("## 一、")   # 位于文首
    md2 = _render(tmp_path, flags=[])
    assert "数据核查提示" not in md2


# ---------- 配图 ----------

def test_figs_inserted_at_chapter_end_with_relative_path(tmp_path):
    rdir = tmp_path / "r"
    figs_dir = rdir / "figs"
    figs_dir.mkdir(parents=True, exist_ok=True)
    f1 = figs_dir / "ch01_revenue.png"
    f2 = figs_dir / "ch02_output.png"
    f1.write_bytes(b"\x89PNG\r\n\x1a\n")                   # 占位, 渲染只查存在性
    f2.write_bytes(b"\x89PNG\r\n\x1a\n")
    md = _render(tmp_path, figs=[(f1, 1, "图1 营收五年走势"), (f2, 2)])
    ch1, ch2 = _section(md, "一、"), _section(md, "二、")
    assert ch1.rstrip().endswith("*图1 营收五年走势*")       # 章末图 + 说明行
    assert "![](figs/ch01_revenue.png)" in ch1              # 绝对路径 → 相对引用
    assert "![](figs/ch02_output.png)" in ch2
    assert "![](figs/ch01_revenue.png)" not in ch2


def test_unbound_fig_goes_to_appendix_section(tmp_path):
    rdir = tmp_path / "r"
    rdir.mkdir(parents=True, exist_ok=True)
    loose = rdir / "misc_snapshot.png"                    # 无章号且文件名无 chNN
    loose.write_bytes(b"\x89PNG\r\n\x1a\n")
    md = _render(tmp_path, figs=[loose])
    assert "![](misc_snapshot.png)" in _section(md, "附图")
    assert "misc_snapshot" not in _section(md, "一、")


# ---------- 附录 ----------

def test_appendix_hypotheses_methodology_disclaimer(tmp_path):
    md = _render(tmp_path)
    ap = _section(md, "附录 A")
    assert "2026 年茅台酒销量增速不低于 4%" in ap and "待验证" in ap
    assert "获支持" in ap and "T:tushare.revenue.2025A" in ap   # related_eids 可溯
    assert "i茅台酒平台销量披露" in ap
    assert r"直销占比继续提升\|带动毛利率上行" in ap             # 竖线转义不破表
    assert "三层分离" in _section(md, "附录 B")
    disc = _section(md, "免责声明")
    assert "不构成任何投资建议" in disc and "脚注" in disc


# ---------- 总装 CLI (离线: 分片+草稿已落盘) ----------

def _make_research_dir(root: Path) -> Path:
    rdir = root / "research" / "600519.SH"
    rdir.mkdir(parents=True, exist_ok=True)
    save_shard(EvidenceShard(kind="tushare", ts_code="600519.SH", facts=_facts()),
               rdir / "facts_tushare.json")
    save_shard(EvidenceShard(kind="reports", ts_code="600519.SH", views=_views()),
               rdir / "views_reports.json")
    save_draft(_draft(), rdir / "draft.json")
    (rdir / "figs").mkdir(exist_ok=True)
    (rdir / "figs" / "ch01_revenue.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return rdir


def test_cli_only_render_offline(tmp_path, capsys):
    _make_research_dir(tmp_path)
    rc = cli_main(["600519.SH", "--only", "render", "--out-root", str(tmp_path)])
    assert rc == 0
    out_txt = capsys.readouterr().out
    assert "研究报告已生成" in out_txt
    report = tmp_path / "research" / "600519.SH" / f"report_{date.today().isoformat()}.md"
    md = report.read_text(encoding="utf-8")
    assert md.count("<sup>[") >= 5 and "![](figs/ch01_revenue.png)" in md


def test_cli_render_resolves_stock_name_from_draft(tmp_path, capsys):
    _make_research_dir(tmp_path)
    rc = cli_main(["贵州茅台", "--only", "render", "--out-root", str(tmp_path)])
    assert rc == 0
    capsys.readouterr()
    report = tmp_path / "research" / "600519.SH" / f"report_{date.today().isoformat()}.md"
    assert report.is_file()


def test_cli_render_without_draft_exits(tmp_path):
    (tmp_path / "research" / "600519.SH").mkdir(parents=True)
    with pytest.raises(SystemExit):
        cli_main(["600519.SH", "--only", "render", "--out-root", str(tmp_path)])
    with pytest.raises(SystemExit):
        cli_main(["未知股票", "--only", "render", "--out-root", str(tmp_path)])
