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
import re
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


# ---------- 校验与统计 (T01: 分片/草稿进入下一 Stage 前的机器检查, 纯逻辑) ----------

# period 合法格式: YYYY / YYYYA / YYYYH1 / YYYYQn(n=1-4) / YYYY-MM-DD (真实日期)
_PERIOD_RE = re.compile(
    r"^(?:\d{4}|\d{4}A|\d{4}H1|\d{4}Q[1-4]"
    r"|\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))$"
)
_HAS_DIGIT = re.compile(r"\d")
_CITE_RE = re.compile(r"\[(E|V):([^\]\s]+)\]")   # 行内引用 [E:eid] / [V:vid]

_FACT_EID_PREFIXES = ("T:", "A:")   # T:Tushare 域 / A:年报
_VIEW_VID_PREFIX = "R:"             # R:研报


def valid_period(period: str) -> bool:
    """期间格式合法: YYYY / YYYYA / YYYYH1 / YYYYQn / YYYY-MM-DD (月日须真实)"""
    if not _PERIOD_RE.match(period):
        return False
    if len(period) == 10:           # 日期型再挡 02-30 之类"格式对但非真实日期"
        try:
            datetime.strptime(period, "%Y-%m-%d")
        except ValueError:
            return False
    return True


def validate_shard(shard: EvidenceShard) -> List[str]:
    """校验证据分片合法性, 返回错误清单 (空=合法)。

    规则: eid/vid 分片内唯一且前缀合法 (T:/A: 为 fact, R: 为 view);
    Fact.layer=="fact"; View.org 与 publish_date 非空; period 非空时格式合法;
    content 含数字时应有 value 或 unit 非空。
    """
    errors: List[str] = []
    seen_eids: set = set()
    for i, f in enumerate(shard.facts):
        where = f"[{shard.kind}] facts[{i}]"
        if f.eid in seen_eids:
            errors.append(f"{where} eid 重复: {f.eid!r}")
        seen_eids.add(f.eid)
        if not f.eid.startswith(_FACT_EID_PREFIXES):
            errors.append(f"{where} eid 前缀非法: {f.eid!r} (须 T:/A:)")
        if f.layer != "fact":
            errors.append(f"{where} layer 必须为 'fact', 实为 {f.layer!r}")
        if f.period and not valid_period(f.period):
            errors.append(f"{where} period 格式非法: {f.period!r} "
                          f"(合法: YYYY/YYYYA/YYYYH1/YYYYQn/YYYY-MM-DD)")
        if _HAS_DIGIT.search(f.content) and f.value is None and not f.unit:
            errors.append(f"{where} content 含数字但 value 与 unit 均空: {f.eid!r}")
    seen_vids: set = set()
    for i, v in enumerate(shard.views):
        where = f"[{shard.kind}] views[{i}]"
        if v.vid in seen_vids:
            errors.append(f"{where} vid 重复: {v.vid!r}")
        seen_vids.add(v.vid)
        if not v.vid.startswith(_VIEW_VID_PREFIX):
            errors.append(f"{where} vid 前缀非法: {v.vid!r} (须 R:)")
        if not v.org:
            errors.append(f"{where} View.org 为空 (观点必须可归属机构)")
        if not v.publish_date:
            errors.append(f"{where} View.publish_date 为空 (观点必须可定位日期)")
        if v.period and not valid_period(v.period):
            errors.append(f"{where} period 格式非法: {v.period!r} "
                          f"(合法: YYYY/YYYYA/YYYYH1/YYYYQn/YYYY-MM-DD)")
    return errors


def validate_draft(draft: Draft, facts: List[Fact], views: List[View]) -> List[str]:
    """校验草稿引用闭环: used_eids 与正文行内 [E:eid]/[V:vid] 必须存在于证据集;
    hypotheses 每条至少 1 个非空 indicator。返回错误清单 (空=合法)。"""
    errors: List[str] = []
    eid_set = {f.eid for f in facts}
    vid_set = {v.vid for v in views}
    for ch in draft.chapters:
        where = f"[draft] 第{ch.no}章<{ch.title}>"
        for ref in ch.used_eids:
            if ref.startswith(_FACT_EID_PREFIXES):
                if ref not in eid_set:
                    errors.append(f"{where} used_eids 引用不存在的事实: {ref!r}")
            elif ref.startswith(_VIEW_VID_PREFIX):
                if ref not in vid_set:
                    errors.append(f"{where} used_eids 引用不存在的观点: {ref!r}")
            else:
                errors.append(f"{where} used_eids 前缀非法: {ref!r} (须 T:/A:/R:)")
        for m in _CITE_RE.finditer(ch.body_md):
            pool = eid_set if m.group(1) == "E" else vid_set
            if m.group(2) not in pool:
                errors.append(f"{where} 行内引用 {m.group(0)} 不在证据集")
    for h in draft.hypotheses:
        if not [s for s in h.indicators if s and s.strip()]:
            errors.append(f"[draft] 假设 {h.hid!r} 缺验证指标 (indicators 至少 1 条非空)")
    return errors


def pack_stats(facts: List[Fact], views: List[View]) -> Dict[str, Any]:
    """证据包统计: 按来源前缀 (T=tushare/A=annual/R=reports)/期间/观点机构分布 — 供 T09 评分卡与调试。"""
    def kind_of(ref: str) -> str:
        if ref.startswith("T:"):
            return "tushare"
        if ref.startswith("A:"):
            return "annual"
        if ref.startswith("R:"):
            return "reports"
        return "other"

    def bump(counter: Dict[str, int], key: str) -> None:
        counter[key] = counter.get(key, 0) + 1

    facts_by_kind: Dict[str, int] = {}
    facts_by_period: Dict[str, int] = {}
    for f in facts:
        bump(facts_by_kind, kind_of(f.eid))
        bump(facts_by_period, f.period or "unknown")
    views_by_kind: Dict[str, int] = {}
    views_by_period: Dict[str, int] = {}
    views_by_org: Dict[str, int] = {}
    for v in views:
        bump(views_by_kind, kind_of(v.vid))
        bump(views_by_period, v.period or "unknown")
        bump(views_by_org, v.org or "unknown")
    return {
        "fact_count": len(facts),
        "view_count": len(views),
        "numeric_fact_count": sum(1 for f in facts if f.value is not None),
        "facts_by_kind": facts_by_kind,
        "views_by_kind": views_by_kind,
        # 机构覆盖分布 (观点多样性供评分卡): 计数降序、同数按名称升序
        "views_by_org": dict(sorted(views_by_org.items(), key=lambda kv: (-kv[1], kv[0]))),
        "facts_by_period": dict(sorted(facts_by_period.items())),
        "views_by_period": dict(sorted(views_by_period.items())),
    }
