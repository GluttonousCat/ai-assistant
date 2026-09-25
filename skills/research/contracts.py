# -*- encoding: utf-8 -*-
"""
skills/research 契约层 — 研究生产线的共享数据结构 (唯一事实源)

三个 Stage 之间全部数据交换通过本模块数据类; 各任务模块只 import, 不重复定义。
契约由 T01 任务独占维护 (微调需在 docs/research/tasks/T00 追加变更记录)。

三层内容分离 (方法论: docs/research/company_industry_guide.md):
  fact      事实层: 可回溯到数据源 (Tushare 字段/年报原文/公告)
  view      观点层: 第三方研报主张 (org+date 必带, 永不当作事实引用)
  judgment  判断层: 本系统生成的假设/情景 (必带验证指标)

落盘布局 (output/research/<ts_code>/, 分片独立文件, 并行任务互不写同一文件):
  facts_tushare.json     T02 产
  facts_annual_<年份>.json  T03 产
  views_reports.json     T04 产
  draft.json             T06 产
  figs/ch*.png           T07 产
  report_<日期>.md       T08 产
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LAYERS = ("fact", "view", "judgment")


# ---------- 证据与观点 ----------

@dataclass
class Fact:
    """事实层证据原子: 一条可回溯的事实陈述 (数值型必带 value+unit+period)"""
    eid: str                        # 全局唯一: "T:tushare.revenue.2025A" / "A:2025.mdna.sub3"
    content: str                    # 事实陈述 (自含数值+单位+期间, 单句)
    source: str                     # 回溯描述: "fin.income.revenue" / "2025年报·管理层讨论·三、经营情况"
    layer: str = "fact"             # 恒为 fact
    ts_code: str = ""
    period: str = ""                # "2025A" / "2025Q3" / "2025-12-31"
    value: Optional[float] = None   # 数值型事实的值 (供作图/排序)
    unit: str = ""                  # 亿元 / % / 万片
    confidence: float = 1.0         # 原文直引=1.0; 换算/推算=0.9 并在 source 注明公式


@dataclass
class View:
    """观点层: 研报主张 (转述不改口径); 渲染时必须标注机构+日期, 严禁作事实句引用"""
    vid: str                        # "R:<report_id>.<seq>"
    statement: str                  # 主张转述 (单句, 保留口径)
    org: str                        # 机构中文通用名 (经 core.helpers.normalize_org_name 归一)
    publish_date: str = ""
    report_id: int = 0
    layer: str = "view"             # 恒为 view
    ts_code: str = ""
    period: str = ""                # 主张涉及期间
    category: str = ""              # viewpoint / forecast / rating / risk


@dataclass
class Hypothesis:
    """判断层: 可验证的研究假设 (指南要求 — 假设→指标→验证闭环)"""
    hid: str
    statement: str
    indicators: List[str] = field(default_factory=list)   # 验证指标 (含数据来源提示)
    related_eids: List[str] = field(default_factory=list)
    status: str = "open"           # open / supported / refuted / needs_review


# ---------- Stage 间交换物 ----------

@dataclass
class EvidenceShard:
    """证据分片: 一个数据源一个文件, 并行任务各写各的, load_pack 汇总"""
    kind: str                       # tushare / annual / reports
    ts_code: str
    facts: List[Fact] = field(default_factory=list)
    views: List[View] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)    # 生成时间/覆盖说明/抽取统计


@dataclass
class Chapter:
    """Stage 2 产出的一章 (章序与模板见 prompts 任务 T05)"""
    no: int                         # 1..10
    title: str
    body_md: str                    # 正文; 行内引用标注 [E:eid] / [V:vid]
    used_eids: List[str] = field(default_factory=list)


@dataclass
class Draft:
    """draft.json — Stage 2 完整产出"""
    ts_code: str
    name: str
    generated_at: str = ""
    chapters: List[Chapter] = field(default_factory=list)
    hypotheses: List[Hypothesis] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)        # 引用校验器告警 (T06)


# ---------- 序列化与汇总 ----------

_DC = {"shard": EvidenceShard, "draft": Draft}


def _from(cls, d: Dict[str, Any]):
    """dict -> dataclass (忽略未知键; 嵌套列表递归水合)"""
    nested = {"facts": Fact, "views": View, "chapters": Chapter,
              "hypotheses": Hypothesis}
    names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    obj = cls(**{k: v for k, v in d.items() if k in names})
    for fname, fcls in nested.items():
        if hasattr(obj, fname):
            setattr(obj, fname, [fcls(**{k: v for k, v in (x if isinstance(x, dict)
                                                            else asdict(x)).items()
                                          if k in {f.name for f in fcls.__dataclass_fields__.values()}})
                                 for x in getattr(obj, fname)])
    return obj


def save_shard(shard: EvidenceShard, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(shard), ensure_ascii=False, indent=1),
                    encoding="utf-8")


def load_shard(path: Path) -> EvidenceShard:
    return _from(EvidenceShard, json.loads(path.read_text(encoding="utf-8")))


def load_pack(research_dir: Path) -> Tuple[List[Fact], List[View]]:
    """汇总目录下所有证据分片 -> (facts, views); eid/vid 重复以先到者为准并忽略后到"""
    facts: List[Fact] = []
    views: List[View] = []
    seen_f, seen_v = set(), set()
    for p in sorted(research_dir.glob("*.json")):
        if p.name.startswith(("draft", "report")):
            continue
        try:
            shard = load_shard(p)
        except Exception:            # 非分片文件跳过
            continue
        for f in shard.facts:
            fd = _from(Fact, f if isinstance(f, dict) else asdict(f))
            if fd.eid not in seen_f:
                seen_f.add(fd.eid)
                facts.append(fd)
        for v in shard.views:
            vd = _from(View, v if isinstance(v, dict) else asdict(v))
            if vd.vid not in seen_v:
                seen_v.add(vd.vid)
                views.append(vd)
    return facts, views


def save_draft(draft: Draft, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(draft), ensure_ascii=False, indent=1),
                    encoding="utf-8")


def load_draft(path: Path) -> Draft:
    return _from(Draft, json.loads(path.read_text(encoding="utf-8")))


def research_dir(root_out: Path, ts_code: str) -> Path:
    """output/research/<ts_code>/"""
    return root_out / "research" / ts_code
