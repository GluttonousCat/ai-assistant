# -*- encoding: utf-8 -*-
"""
T09 · quality scorecard — turns "a quality research report" into a measurable score
(task spec: docs/research/tasks/T09)

    score(draft, facts, views, flags) -> {"score": 0-100, "pass": bool, "dimensions": {...}}

Six dimensions (passing lines copied from the task spec; weights self-chosen, rationale below):

  dimension         measure                                              line     weight
  evidence_coverage coverage: share of numeric sentences with [E:]/[V:]   >=80%     30
  citation_validity citation validity: ghost citations (self-check ∪ flags) =0       20
  structure         completeness: chapters 1..10 covered (flags-declared skips excepted) 10/10  15
  evidence_richness richness: total facts >=100 and annual (A:) facts >=15  both met 15
  hypothesis        verifiability: share of hypotheses with >=1 non-empty indicator  =100%    10
  layer_purity      three-layer purity: sentences with [V: but no org attribution   <=2      10

Weight rationale: the citation loop (coverage 30 + validity 20 = 50 pts) directly measures
the first iron rule "evidence must be traceable" and takes half the total — ghost citations
are hard defects, so that dimension zeroes out entirely; structure and evidence supply
(15+15) are the report's skeleton and raw material; hypothesis verifiability (10) and
three-layer purity (10) map to the methodology's "views never masquerade as facts /
hypotheses are falsifiable", one-tenth each.

Measurement conventions (boundary of the split with T06/T08):
  - sentence: split on 。！？!?；;\n; markdown heading lines (#), table lines (|) and code
    fence lines (`) are not counted — numbers inside tables are constrained indirectly by
    evidence_richness and citation validity (known limitation, see the "问题" section of
    docs/research/验收_贵州茅台.md).
  - numeric sentence: counts only when digits (ASCII/full-width) remain after stripping
    inline citation markers — digits inside the [E:T:2025.x] marker itself don't count;
    "表2/图3/第4章" are cross-references rather than data claims and are stripped first.
  - three-layer purity: a sentence containing [V:vid] must, after stripping markers,
    contain the cited view's org name (core word with common suffixes like 证券/公司/研究所
    stripped, >=2 chars) or a generic attribution word (机构/券商/研报/分析师/卖方…).
    Opinion verbs (认为/预计…) do not constitute attribution — otherwise views would be
    free to masquerade as facts.
  - ghost citation: recomputed independently of T06 (used_eids + inline body citations;
    anything absent from the matching evidence set is a ghost, illegal prefixes count too),
    then citation markers are leniently extracted from flag texts and unioned in — the T06
    message format is not fixed; when no markers can be extracted only the self-check
    remains, which does not affect correctness.
  - flags of None falls back to draft.flags (warnings T06 left on the Draft).

CLI: python -m skills.research.quality_rubric <ts_code or research dir> [--json]
     scores output/research/<ts_code>/'s draft.json + evidence shards, prints the score report.
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

# ---------- constants (passing lines) ----------

COVERAGE_LINE = 0.80      # evidence coverage >=80%
PURITY_LINE = 2           # view sentences without org attribution <=2
RICHNESS_FACT_LINE = 100  # total facts >=100
RICHNESS_ANNUAL_LINE = 15  # annual-report (A: prefix) facts >=15
TOTAL_CHAPTERS = 10       # ten-chapter template

EXPECTED_CHAPTERS = tuple(range(1, TOTAL_CHAPTERS + 1))

# Same shape as contracts._CITE_RE (T01 owns the contract; mirrored here, never modified)
_CITE_RE = re.compile(r"\[(E|V):([^\]\s]+)\]")
_V_CITE_RE = re.compile(r"\[V:([^\]\s]+)\]")
_SENT_SPLIT_RE = re.compile(r"[。！？!?\n；;]")
_DIGIT_RE = re.compile(r"[0-9０-９]")
# Cross-reference noise (表2/图3/第4章/第4节…): prose pointers, not data claims
_XREF_RE = re.compile(r"[表图]\s*\d+|第\s*\d+\s*[章节部分]")
# Common org-name suffixes — strip them, then match on the core word (中金公司→中金, 国泰君安证券→国泰君安)
_ORG_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司",
                 "证券", "公司", "研究所", "国际", "资本", "投资", "集团")
# Generic attribution words: collective citations like "多家券商认为…" also count as attributed
_ATTRIBUTION_RE = re.compile(r"机构|券商|研报|研究报告|分析师|卖方|研究部|策略团队|头部买方")
# Skip-chapter declarations in flags: must hit both a chapter number and a skip keyword (lenient about the T06 message format)
_CH_NO_RE = re.compile(r"第\s*(\d{1,2})\s*[章节]|ch(?:apter)?\s*[-_ ]?(\d{1,2})", re.I)
_SKIP_KW_RE = re.compile(r"跳过|缺失|未生成|略去|省略|skip", re.I)


# ---------- sentence enumeration and predicates ----------

def _iter_sentences(body_md: str) -> Iterable[str]:
    """Yield body-chapter sentences: skip markdown heading/table/code-fence lines and blanks, split on Chinese/English sentence punctuation."""
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
    """Plain text after stripping inline citation markers and cross-reference noise"""
    return _XREF_RE.sub("", _CITE_RE.sub(" ", sent))


def _is_numeric_sentence(sent: str) -> bool:
    return bool(_DIGIT_RE.search(_plain(sent)))


def _cited_marks(sent: str) -> List[Tuple[str, str]]:
    """All inline citations in the sentence -> [("E", ref), ("V", ref), ...]"""
    return [(m.group(1), m.group(2)) for m in _CITE_RE.finditer(sent)]


# ---------- three-layer purity ----------

def _org_cores(org: str) -> Set[str]:
    """Candidate core words of an org name (original + suffix-stripped forms, at least 2 chars)"""
    cores = {org}
    for suf in _ORG_SUFFIXES:
        if org.endswith(suf) and len(org) - len(suf) >= 2:
            cores.add(org[: len(org) - len(suf)])
    return {c for c in cores if len(c) >= 2}


def _view_attributed(sent: str, vids: List[str],
                     view_by_vid: Dict[str, View]) -> bool:
    """Whether a view sentence is attributed: the cited org name (core word) appears, or a generic attribution word is present."""
    plain = _plain(sent)
    for vid in vids:
        v = view_by_vid.get(vid)
        if v is None:
            continue  # ghost citations are handled by the citation_validity dimension; no double penalty here
        if any(core in plain for core in _org_cores(v.org)):
            return True
    return bool(_ATTRIBUTION_RE.search(plain))


# ---------- ghost citations and skipped chapters ----------

def _collect_ghosts(draft: Draft, facts: List[Fact], views: List[View],
                    flags: List[str]) -> Set[str]:
    """Recompute ghost citations (independent of T06): inline body citations + used_eids +
    citations reported in flags; anything absent from the matching evidence set is a ghost;
    illegal prefixes (not T:/A:/R:) count too."""
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
    """Chapter numbers declared skipped in flags (chapter-number pattern + skip keyword both hit)"""
    out: Set[int] = set()
    for fl in flags:
        if not _SKIP_KW_RE.search(fl):
            continue
        for m in _CH_NO_RE.finditer(fl):
            out.add(int(m.group(1) or m.group(2)))
    return out


# ---------- scoring main entry ----------

def score(draft: Draft, facts: List[Fact], views: List[View],
          flags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Six-dimension weighted score. Returns {"score": 0-100, "pass": bool, "dimensions": {...}};
    pass = every dimension meets its passing line (independent of the total, which exists
    for cross-sectional comparison granularity)."""
    if flags is None:
        flags = list(draft.flags)

    # 1) evidence coverage + three-layer purity (one sentence scan)
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
    coverage = (num_cited / num_total) if num_total else 1.0  # no numeric sentences = vacuous pass

    # 2) citation validity
    ghosts = _collect_ghosts(draft, facts, views, flags)

    # 3) structural completeness
    produced = {ch.no for ch in draft.chapters}
    skipped = _skipped_chapter_nos(flags)
    expected = set(EXPECTED_CHAPTERS)
    covered = (produced | skipped) & expected
    missing = sorted(expected - covered)

    # 4) evidence richness
    fact_total = len(facts)
    annual_total = sum(1 for f in facts if f.eid.startswith("A:"))

    # 5) hypothesis verifiability
    hyp_total = len(draft.hypotheses)
    hyp_ok = sum(1 for h in draft.hypotheses
                 if any(s and s.strip() for s in h.indicators))
    hyp_ratio = (hyp_ok / hyp_total) if hyp_total else 0.0  # no hypotheses = judgment layer missing

    # 6) three-layer purity scoring: each unattributed sentence costs 20%, 5 sentences zero it out (line <=2 -> at least 60% retained)
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


# ---------- directory-level entry and report ----------

def score_from_dir(research_dir: Path,
                   flags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Score output/research/<ts_code>/ (draft.json + evidence shards)"""
    draft_path = research_dir / "draft.json"
    if not draft_path.is_file():
        raise FileNotFoundError(f"未找到 {draft_path} — 先跑 Stage 2 生成草稿")
    draft = load_draft(draft_path)
    facts, views = load_pack(research_dir)
    return score(draft, facts, views, flags)


def format_report(result: Dict[str, Any]) -> str:
    """Score result -> Chinese markdown report (shared by acceptance docs / CLI)"""
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
