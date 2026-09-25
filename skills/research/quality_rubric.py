# -*- encoding: utf-8 -*-
"""
T09 · 质量评分卡 — 把"优质研究报告"变成可度量的分数 (任务书: docs/research/tasks/T09)

    score(draft, facts, views, flags) -> {"score": 0-100, "pass": bool, "dimensions": {...}}

六个维度 (达标线抄自任务书; 权重自定, 理由见下):

  维度            度量                                        达标线        权重
  evidence_coverage 证据覆盖率: 数字句中带 [E:]/[V:] 引用比例      ≥80%         30
  citation_validity 引用有效性: 幽灵引用数 (自查 ∪ flags 报告)      =0           20
  structure         结构完整度: 第1..10章覆盖 (flags 声明跳过除外)  10/10        15
  evidence_richness 证据丰度: facts 总数 ≥100 且年报(A:)事实 ≥15   双达标        15
  hypothesis        假设可验证率: hypotheses 含 ≥1 非空指标比例      =100%        10
  layer_purity      三层纯净度: 含 [V: 却无机构标注的句子数          ≤2           10

权重理由: 引用闭环 (覆盖率30 + 有效性20 = 50分) 是"证据可回溯"这条第一铁律的直接
度量, 占一半 — 幽灵引用是硬伤故整维清零; 结构与证据供给 (15+15) 是报告的骨架与
原料; 假设可验证 (10) 与三层纯净 (10) 对应方法论"观点不冒充事实/假设可证伪", 各一成。

口径约定 (与 T06/T08 的分工边界):
  - 句子: 按 。！？!?；;\n 切分; markdown 标题行(#)、表格行(|)、代码围栏行(`) 不计
    入句子 — 表格中的数字由 evidence_richness 与引用有效性间接约束 (已知局限,
    见 docs/research/验收_贵州茅台.md "问题"一节)。
  - 数字句: 剥除行内引用记号后仍含数字 (ASCII/全角) 才算 — [E:T:2025.x] 记号本身
    的数字不计; "表2/图3/第4章" 属交叉引用非数据主张, 先剥除再判定。
  - 三层纯净: 含 [V:vid] 的句子剥记号后, 须出现被引观点机构名 (剥"证券/公司/研究
    所"等常见后缀的核心词, ≥2字) 或泛指归属词 (机构/券商/研报/分析师/卖方…)。
    观点动词 (认为/预计…) 不构成归属 — 否则等于放任观点伪装成事实。
  - 幽灵引用: 独立于 T06 复算 (used_eids + 正文行内引用, 不在证据集即幽灵, 前缀
    非法同样计入), 再从 flags 文本中宽容提取引用记号取并集 — T06 消息格式未定,
    提不出记号就只剩自查结果, 不影响正确性。
  - flags 为 None 时取 draft.flags (T06 落在 Draft 上的告警)。

CLI: python -m skills.research.quality_rubric <ts_code 或研究目录> [--json]
     对 output/research/<ts_code>/ 的 draft.json + 证据分片跑分, 输出评分报告。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from core.logger import get_logger
from skills.research.contracts import Draft, Fact, View, load_draft, load_pack

logger = get_logger(__name__)

# ---------- 常量 (达标线) ----------

COVERAGE_LINE = 0.80      # 证据覆盖率 ≥80%
PURITY_LINE = 2           # 无机构标注的观点句 ≤2
RICHNESS_FACT_LINE = 100  # facts 总数 ≥100
RICHNESS_ANNUAL_LINE = 15  # 年报(A:前缀)事实 ≥15
TOTAL_CHAPTERS = 10       # 十章模板

EXPECTED_CHAPTERS = tuple(range(1, TOTAL_CHAPTERS + 1))

# 与 contracts._CITE_RE 同构 (T01 独占契约, 此处只镜像不改)
_CITE_RE = re.compile(r"\[(E|V):([^\]\s]+)\]")
_V_CITE_RE = re.compile(r"\[V:([^\]\s]+)\]")
_SENT_SPLIT_RE = re.compile(r"[。！？!?\n；;]")
_DIGIT_RE = re.compile(r"[0-9０-９]")
# 交叉引用噪声 (表2/图3/第4章/第4节…): 是行文指针不是数据主张
_XREF_RE = re.compile(r"[表图]\s*\d+|第\s*\d+\s*[章节部分]")
# 机构名常见后缀 — 剥掉后取核心词做包含匹配 (中金公司→中金, 国泰君安证券→国泰君安)
_ORG_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司",
                 "证券", "公司", "研究所", "国际", "资本", "投资", "集团")
# 泛指归属词: "多家券商认为…"式集体引用也算有归属
_ATTRIBUTION_RE = re.compile(r"机构|券商|研报|研究报告|分析师|卖方|研究部|策略团队|头部买方")
# flags 中的跳章声明: 须同时命中章号与跳过关键词 (对 T06 消息格式宽容)
_CH_NO_RE = re.compile(r"第\s*(\d{1,2})\s*[章节]|ch(?:apter)?\s*[-_ ]?(\d{1,2})", re.I)
_SKIP_KW_RE = re.compile(r"跳过|缺失|未生成|略去|省略|skip", re.I)


# ---------- 句子枚举与判定 ----------

def _iter_sentences(body_md: str) -> Iterable[str]:
    """产出正文章节句子: 跳过 markdown 标题/表格/代码围栏内行与空行, 按中英文句读切分。"""
    in_fence = False
    for raw in body_md.split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line[0] in "#|`":
            continue
        for sent in _SENT_SPLIT_RE.split(line):
            s = sent.strip().lstrip("-*•·> ").rstrip()
            if s:
                yield s


def _plain(sent: str) -> str:
    """剥行内引用记号与交叉引用噪声后的纯文本"""
    return _XREF_RE.sub("", _CITE_RE.sub(" ", sent))


def _is_numeric_sentence(sent: str) -> bool:
    return bool(_DIGIT_RE.search(_plain(sent)))


def _cited_marks(sent: str) -> List[Tuple[str, str]]:
    """句中全部行内引用 -> [("E", ref), ("V", ref), ...]"""
    return [(m.group(1), m.group(2)) for m in _CITE_RE.finditer(sent)]


# ---------- 三层纯净度 ----------

def _org_cores(org: str) -> Set[str]:
    """机构名的候选核心词 (原名 + 剥常见后缀, 至少 2 字)"""
    cores = {org}
    for suf in _ORG_SUFFIXES:
        if org.endswith(suf) and len(org) - len(suf) >= 2:
            cores.add(org[: len(org) - len(suf)])
    return {c for c in cores if len(c) >= 2}


def _view_attributed(sent: str, vids: List[str],
                     view_by_vid: Dict[str, View]) -> bool:
    """观点句是否有归属: 被引机构名 (核心词) 出现, 或有泛指归属词。"""
    plain = _plain(sent)
    for vid in vids:
        v = view_by_vid.get(vid)
        if v is None:
            continue  # 幽灵引用由 citation_validity 维度处理, 此处不重复计罚
        if any(core in plain for core in _org_cores(v.org)):
            return True
    return bool(_ATTRIBUTION_RE.search(plain))


# ---------- 幽灵引用与跳章 ----------

def _collect_ghosts(draft: Draft, facts: List[Fact], views: List[View],
                    flags: List[str]) -> Set[str]:
    """复算幽灵引用 (独立于 T06): 正文行内引用 + used_eids + flags 中报告的引用,
    不在对应证据集即幽灵; 前缀非法 (非 T:/A:/R:) 同样计入。"""
    eid_set = {f.eid for f in facts}
    vid_set = {v.vid for v in views}
    ghosts: Set[str] = set()

    def _check(ref: str) -> None:
        if ref.startswith(("T:", "A:")):
            if ref not in eid_set:
                ghosts.add(ref)
        elif ref.startswith("R:"):
            if ref not in vid_set:
                ghosts.add(ref)
        else:
            ghosts.add(ref)

    for ch in draft.chapters:
        for m in _CITE_RE.finditer(ch.body_md):
            _check(m.group(2))
        for ref in ch.used_eids:
            _check(ref)
    for fl in flags:
        for m in _CITE_RE.finditer(fl):
            _check(m.group(2))
    return ghosts


def _skipped_chapter_nos(flags: List[str]) -> Set[int]:
    """flags 中声明跳过的章号 (第N章/章N + 跳过关键词 同时命中)"""
    out: Set[int] = set()
    for fl in flags:
        if not _SKIP_KW_RE.search(fl):
            continue
        for m in _CH_NO_RE.finditer(fl):
            out.add(int(m.group(1) or m.group(2)))
    return out


# ---------- 评分主入口 ----------

def score(draft: Draft, facts: List[Fact], views: List[View],
          flags: Optional[List[str]] = None) -> Dict[str, Any]:
    """六维加权评分。返回 {"score": 0-100, "pass": bool, "dimensions": {...}};
    pass = 六维达标线全部满足 (与总分独立, 总分用于横向比较颗粒度)。"""
    if flags is None:
        flags = list(draft.flags)

    # 1) 证据覆盖率 + 三层纯净度 (同一次句子扫描)
    view_by_vid = {v.vid: v for v in views}
    num_total = num_cited = 0
    impure: List[Dict[str, Any]] = []
    per_chapter: List[Dict[str, Any]] = []
    for ch in draft.chapters:
        ch_num = ch_num_cited = 0
        for sent in _iter_sentences(ch.body_md):
            marks = _cited_marks(sent)
            vids = [ref for kind, ref in marks if kind == "V"]
            if _is_numeric_sentence(sent):
                num_total += 1
                ch_num += 1
                if marks:
                    num_cited += 1
                    ch_num_cited += 1
            known_vids = [vid for vid in vids if vid in view_by_vid]
            if known_vids and not _view_attributed(sent, known_vids, view_by_vid):
                impure.append({"chapter": ch.no, "sentence": sent[:80]})
        per_chapter.append({"chapter": ch.no, "numeric": ch_num, "cited": ch_num_cited})
    coverage = (num_cited / num_total) if num_total else 1.0  # 无数字句=空转通过

    # 2) 引用有效性
    ghosts = _collect_ghosts(draft, facts, views, flags)

    # 3) 结构完整度
    produced = {ch.no for ch in draft.chapters}
    skipped = _skipped_chapter_nos(flags)
    expected = set(EXPECTED_CHAPTERS)
    covered = (produced | skipped) & expected
    missing = sorted(expected - covered)

    # 4) 证据丰度
    fact_total = len(facts)
    annual_total = sum(1 for f in facts if f.eid.startswith("A:"))

    # 5) 假设可验证率
    hyp_total = len(draft.hypotheses)
    hyp_ok = sum(1 for h in draft.hypotheses
                 if any(s and s.strip() for s in h.indicators))
    hyp_ratio = (hyp_ok / hyp_total) if hyp_total else 0.0  # 无假设=判断层缺失

    # 6) 三层纯净度计分: 每句无归属扣 20%, 5 句扣光 (达标线 ≤2 → 至少保 60%)
    purity_ratio = max(0.0, 1.0 - len(impure) / 5.0)

    dims: Dict[str, Dict[str, Any]] = {
        "evidence_coverage": _dim(
            "证据覆盖率", "数字句引用比例 ≥80%", 30, coverage,
            coverage >= COVERAGE_LINE,
            {"numeric_sentences": num_total, "cited_sentences": num_cited,
             "observed": f"数字句引用 {num_cited}/{num_total} ({coverage:.1%})",
             "per_chapter": per_chapter}),
        "citation_validity": _dim(
            "引用有效性", "幽灵引用 =0", 20, 0.0 if ghosts else 1.0,
            not ghosts,
            {"ghost_count": len(ghosts),
             "observed": f"幽灵引用 {len(ghosts)}",
             "ghosts": sorted(ghosts)[:10]}),
        "structure": _dim(
            "结构完整度", "10 章全覆盖 (声明跳过除外)", 15,
            len(covered) / TOTAL_CHAPTERS,
            not missing,
            {"produced": sorted(produced), "skipped_by_flags": sorted(skipped),
             "missing": missing,
             "observed": (f"覆盖 {len(covered)}/10"
                          + (f", 缺 {missing}" if missing else ""))}),
        "evidence_richness": _dim(
            "证据丰度", "facts ≥100 且年报事实 ≥15", 15,
            (min(fact_total / RICHNESS_FACT_LINE, 1.0)
             + min(annual_total / RICHNESS_ANNUAL_LINE, 1.0)) / 2,
            fact_total >= RICHNESS_FACT_LINE and annual_total >= RICHNESS_ANNUAL_LINE,
            {"fact_count": fact_total, "annual_fact_count": annual_total,
             "observed": f"事实 {fact_total} 条, 其中年报 {annual_total} 条"}),
        "hypothesis": _dim(
            "假设可验证率", "含指标假设 =100%", 10, hyp_ratio,
            hyp_total > 0 and hyp_ok == hyp_total,
            {"hypothesis_count": hyp_total, "verifiable": hyp_ok,
             "observed": (f"含指标假设 {hyp_ok}/{hyp_total}"
                          if hyp_total else "无假设 (判断层缺失)")}),
        "layer_purity": _dim(
            "三层纯净度", "无机构标注观点句 ≤2", 10, purity_ratio,
            len(impure) <= PURITY_LINE,
            {"unattributed_view_sentences": len(impure),
             "observed": f"无机构标注观点句 {len(impure)}",
             "samples": impure[:5]}),
    }

    return {
        "score": round(sum(d["weighted"] for d in dims.values()), 1),
        "pass": all(d["pass"] for d in dims.values()),
        "dimensions": dims,
    }


def _dim(label: str, line: str, weight: int, ratio: float, passed: bool,
         detail: Dict[str, Any]) -> Dict[str, Any]:
    return {"label": label, "line": line, "weight": weight,
            "ratio": round(max(0.0, min(ratio, 1.0)), 4),
            "weighted": round(weight * max(0.0, min(ratio, 1.0)), 2),
            "pass": bool(passed), "detail": detail}


# ---------- 目录级入口与报告 ----------

def score_from_dir(research_dir: Path,
                   flags: Optional[List[str]] = None) -> Dict[str, Any]:
    """对 output/research/<ts_code>/ (draft.json + 证据分片) 跑分"""
    draft_path = research_dir / "draft.json"
    if not draft_path.is_file():
        raise FileNotFoundError(f"未找到 {draft_path} — 先跑 Stage 2 生成草稿")
    draft = load_draft(draft_path)
    facts, views = load_pack(research_dir)
    return score(draft, facts, views, flags)


def format_report(result: Dict[str, Any]) -> str:
    """评分结果 -> 中文 markdown 报告 (验收文档/CLI 共用)"""
    verdict = "✅ 通过" if result["pass"] else "❌ 未通过"
    lines = ["## 质量评分卡", "",
             f"**总分 {result['score']:.1f} / 100 · {verdict}**", "",
             "| 维度 | 达标线 | 实测 | 得分 | 通过 |",
             "|---|---|---|---|---|"]
    for d in result["dimensions"].values():
        mark = "✅" if d["pass"] else "❌"
        lines.append(f"| {d['label']} | {d['line']} | {d['detail']['observed']} "
                     f"| {d['weighted']:.1f}/{d['weight']} | {mark} |")
    return "\n".join(lines)


def _main(argv: Optional[List[str]] = None) -> None:
    import argparse
    from core.paths import PathManager

    ap = argparse.ArgumentParser(
        description="T09 质量评分卡: 对研究产物目录跑分 (draft.json + 证据分片)")
    ap.add_argument("target", help="ts_code (如 600519.SH) 或研究目录路径")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON 而非 markdown 表")
    args = ap.parse_args(argv)

    p = Path(args.target)
    research_dir = p if p.is_dir() else Path(
        PathManager().output_dir) / "research" / args.target
    try:
        result = score_from_dir(research_dir)
    except FileNotFoundError as e:
        print(f"[T09] {e}")
        sys.exit(1)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(format_report(result))


if __name__ == "__main__":
    _main()
