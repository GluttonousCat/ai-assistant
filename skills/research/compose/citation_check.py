# -*- encoding: utf-8 -*-
"""Citation checker (T06) — pure logic, no LLM/DB access; every case runs offline.

Runs four checks over the Draft; results come back as a list of flag strings, which
the generator writes into Draft.flags (consumed by the T09 scorecard and the T08
rendering warning zone):

  unverified-number    numeric-sentence heuristic: a sentence (after stripping citation
                       markers and pure year/period expressions) still contains digits
                       while the whole sentence carries no [E:]/[V:] marker -> needs
                       manual verification (sentences explicitly marked「待验证」are exempt)
  ghost-citation       ghost citation: inline body citations / used_eids / hypothesis
                       related_eids point to an eid/vid absent from the evidence pack
                       -> hard defect
  used-eids-mismatch   used_eids inconsistent with inline body citations (one-way
                       omission) -> hard defect
  view-style-hint      loose hint (not enforced): a sentence containing [V:] should
                       carry the institution name or words like「认为/预计」

Flags always use the "category: location description" format, the prefix being the
category so consumers can grade by prefix. Same-category warnings beyond
MAX_FLAGS_PER_CHAPTER per chapter are aggregated into one entry to keep flags from
exploding.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set, Tuple

from skills.research.contracts import Draft, Fact, View

# Inline citation [E:eid] / [V:vid] (same pattern as contracts._CITE_RE; publicly exported here for generator/tests to reuse)
CITE_RE = re.compile(r"\[(E|V):([^\]\s]+)\]")

# Pure year/period expressions: once stripped, no digits remain -> treated as "time
# mention only", not a data assertion.
# Covers: 2025 / 2025年 / 2021-2025 / 2021年-2025年 / 2025A / 2025Q3 / 2025H1 / Q3 / H1 / 3季度
_PERIOD_ONLY_RE = re.compile(
    r"(?:19|20)\d{2}\s*(?:[-—~至]\s*(?:19|20)\d{2})?年?(?:A|Q[1-4]|H1)?"
    r"|[一二三四1-4]\s*季度|(?<![A-Za-z0-9])(?:Q[1-4]|H1)(?![A-Za-z0-9])"
)
_HAS_DIGIT = re.compile(r"\d")

# Sentence boundary: period/exclamation/question mark/newline (semicolons and commas
# do not split — a citation covers the whole sentence)
_SENT_SPLIT = re.compile(r"[。！？!?\n]")
# Non-prose line prefixes: headings and table rows (digits inside them skip the sentence heuristic)
_NON_PROSE_PREFIX = ("#", "|")
_SENT_STRIP = " \t;；,，、-"

# Soft opinion words for view sentences (spec: institution name or "认为"-like wording;
# near-synonyms accepted loosely to avoid false positives)
_VIEW_SOFT_WORDS = ("认为", "预计", "预期", "预测", "判断", "展望")

MAX_FLAGS_PER_CHAPTER = 8    # per-chapter cap on same-category warnings
_DIGEST_LEN = 50             # retained length of a sentence digest


def extract_citations(text: str) -> List[Tuple[str, str]]:
    """Extract inline citations -> [("E", eid), ("V", vid), ...]; duplicates kept in order of appearance."""
    return [(m.group(1), m.group(2)) for m in CITE_RE.finditer(text)]


def iter_sentences(body_md: str) -> Iterable[str]:
    """Markdown body -> sentence stream: skip heading/table lines, then split each line on sentence-final punctuation."""
    for line in body_md.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_NON_PROSE_PREFIX):
            continue
        for sent in _SENT_SPLIT.split(line):
            sent = sent.strip(_SENT_STRIP)
            if sent:
                yield sent


def _digest(s: str) -> str:
    """Sentence digest: strip whitespace then truncate."""
    s = re.sub(r"\s+", "", s)
    return s[:_DIGEST_LEN] + ("…" if len(s) > _DIGEST_LEN else "")


def check_unverified_numbers(no: int, body_md: str) -> List[str]:
    """Numeric-sentence heuristic (chapter no): the sentence makes a data assertion
    (digits remain after removing year/period expressions) yet carries no [E:]/[V:]
    marker at all -> flag (sentences with markers are handled by the existence /
    opinion-style checks instead)."""
    hits: List[str] = []
    for sent in iter_sentences(body_md):
        if CITE_RE.search(sent):
            continue                       # sentence already carries a citation marker
        if "待验证" in sent:
            continue                       # allowed by the rules: an unevidenced number explicitly marked as to-verify
        bare = CITE_RE.sub("", sent)       # citation ids themselves contain digits; strip as a fallback before judging
        if _HAS_DIGIT.search(_PERIOD_ONLY_RE.sub("", bare)):
            hits.append(_digest(bare))
    flags = [f"unverified-number: 第{no}章「{h}」" for h in hits[:MAX_FLAGS_PER_CHAPTER]]
    if len(hits) > MAX_FLAGS_PER_CHAPTER:
        flags.append(f"unverified-number: 第{no}章 另有 {len(hits) - MAX_FLAGS_PER_CHAPTER} 句"
                     f"无引用数字 (已截断)")
    return flags


def check_ghost_citations(draft: Draft, eid_set: Set[str], vid_set: Set[str]) -> List[str]:
    """Ghost citations: inline body citations / used_eids / hypothesis related_eids absent from the evidence pack -> flag."""
    flags: List[str] = []
    for ch in draft.chapters:
        for kind, ref in extract_citations(ch.body_md):
            if ref not in (eid_set if kind == "E" else vid_set):
                flags.append(f"ghost-citation: 第{ch.no}章 行内引用 [{kind}:{ref}] 不在证据集")
        for ref in ch.used_eids:
            if ref not in (vid_set if ref.startswith("R:") else eid_set):
                flags.append(f"ghost-citation: 第{ch.no}章 used_eids 引用 {ref!r} 不在证据集")
    for h in draft.hypotheses:
        for ref in h.related_eids:
            if ref not in (vid_set if ref.startswith("R:") else eid_set):
                flags.append(f"ghost-citation: 假设 {h.hid} related_eids 引用 {ref!r} 不在证据集")
    return flags


def check_used_eids_consistency(draft: Draft) -> List[str]:
    """Cross-check used_eids against the inline body citation set both ways; under- or over-registered entries -> flag."""
    flags: List[str] = []
    for ch in draft.chapters:
        inline = {ref for _, ref in extract_citations(ch.body_md)}
        registered = set(ch.used_eids)
        extra = sorted(registered - inline)
        missing = sorted(inline - registered)
        if extra:
            flags.append(f"used-eids-mismatch: 第{ch.no}章 used_eids 有 {len(extra)} 项正文未引用 "
                         f"({', '.join(extra[:3])}{'…' if len(extra) > 3 else ''})")
        if missing:
            flags.append(f"used-eids-mismatch: 第{ch.no}章 正文引用有 {len(missing)} 项未记入 "
                         f"used_eids ({', '.join(missing[:3])}{'…' if len(missing) > 3 else ''})")
    return flags


def check_view_style(no: int, body_md: str, views: List[View]) -> List[str]:
    """Loose hint: a sentence containing [V:] should mention the cited view's institution name or words like「认为/预计」."""
    vid2org: Dict[str, str] = {v.vid: v.org for v in views}
    hits: List[str] = []
    for sent in iter_sentences(body_md):
        vids = [ref for kind, ref in extract_citations(sent) if kind == "V"]
        if not vids:
            continue
        has_org = any(vid2org.get(v, "") and vid2org[v] in sent for v in vids)
        if not has_org and not any(w in sent for w in _VIEW_SOFT_WORDS):
            hits.append(f"[V:{vids[0]}]「{_digest(CITE_RE.sub('', sent))}」")
    flags = [f"view-style-hint: 第{no}章 {h}" for h in hits[:MAX_FLAGS_PER_CHAPTER]]
    if len(hits) > MAX_FLAGS_PER_CHAPTER:
        flags.append(f"view-style-hint: 第{no}章 另有 {len(hits) - MAX_FLAGS_PER_CHAPTER} 句"
                     f"无机构名/判词 (已截断)")
    return flags


def check_draft(draft: Draft, facts: List[Fact], views: List[View]) -> List[str]:
    """Whole-draft check entry: return order-preserving deduplicated flags (the generator merges them into Draft.flags)."""
    eid_set = {f.eid for f in facts}
    vid_set = {v.vid for v in views}
    flags: List[str] = []
    for ch in draft.chapters:
        flags += check_unverified_numbers(ch.no, ch.body_md)
        flags += check_view_style(ch.no, ch.body_md, views)
    flags += check_ghost_citations(draft, eid_set, vid_set)
    flags += check_used_eids_consistency(draft)
    seen: Set[str] = set()
    return [f for f in flags if not (f in seen or seen.add(f))]
