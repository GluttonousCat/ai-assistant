# -*- encoding: utf-8 -*-
"""
Generic annual-report processing (L0) — a single dialect that covers most
standard-format annual reports, zero LLM.

Division of labor (user decision, 2026-10-12):
- this module = low-cost intelligence: it recognizes only the generic
  structure of the A-share disclosure standard — "第X节/章" chapter anchors +
  the three subsection-anchor forms 一、/(一)/N.M + the chapter-2 alias vocab;
- exceptional structures (A+H governance-code numbering / topic-style titles /
  hybrid forms / TOC traps...) are deliberately not chased: the contract
  machine-check (contracts) flags violations, then the repair Agent (repair)
  provides the semantic fallback = maximum intelligence;
- the mechanical layer (PDF→MD / table parsing / cleaning / assembly) is
  reused from parse.py, not duplicated here.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .parse import (ITEM_ALIASES, SECTION_RULES, _alias_hit, _block_end,
                    _clean_title, _header_lines, _render_body, _sim,
                    _subsection_anchors, _subsection_dict, marker_rank)

# ---------------- chapter layer: the single dialect "第X节/章" ----------------
# Layout tolerance: # prefix (standard) / bare bold '**第X章 …**' (China
# Merchants Bank case, pymupdf left it unmarked) — the structural anchor is
# still the standard 第X节/章 form, not a structural exception; topic-style /
# hybrid / A+H numbering goes to the Agent.
_SECTION_ANCHOR_RE = re.compile(
    r"^[#* \t]*第[一二三四五六七八九十百]+[节章][ \t]*(?P<title>[^\n]*?)[ \t*]*$",
    re.M)

# 1-3 chapter-title lines left over from the TOC page are normal (empirical
# basis for the old >=4 threshold); fewer than that means no standard anchors
_MIN_ANCHORS = 4

# chapter name -> internal key (demand-side vocab, semantically aligned with
# contracts; matched by name, not by chapter number)
_SECTION_KEYS = [
    ("管理层讨论", "mdna"), ("经营业绩", "mdna"), ("业务回顾", "mdna"),
    ("董事会报告", "mdna"), ("经营情况", "mdna"), ("业绩概述", "mdna"),
    ("业务概要", "mdna"), ("未来展望", "mdna"), ("专项分析", "mdna"),
    ("经营分析", "mdna"),
    ("公司治理", "governance"), ("ESG", "governance"),
    ("重要事项", "matters"), ("关联交易", "matters"),
    ("股份变动", "shareholders"), ("股东", "shareholders"),
    ("主要财务数据", "summary"), ("主要会计数据", "summary"),
    ("公司简介", "summary"),
    ("财务报表", "financial"), ("财务报告", "financial"),
    ("释义", "definitions"),
    ("债券", "bonds"),
]


def _section_key(title: str) -> str:
    for kw, key in _SECTION_KEYS:
        if kw in title:
            return key
    return "other"


def _clean_sec_title(title: str) -> str:
    """Chapter-title cleaning: strip checkbox/**/backtick decorations."""
    from .parse import _CHECKBOX
    return re.sub(r"[*`]+", "", _CHECKBOX.sub("", title)).strip()


def split_sections(md: str) -> List[Tuple[str, str, str]]:
    """Split chapters (two generic dialects) → [(internal key, chapter name,
    chapter text)] in document order.

    ① Standard form: "第X节/章" anchors >=4 (1-3 TOC leftovers are normal;
      fewer means none);
    ② Financial topic form: with no standard anchors, take the shallowest
      heading level having >=3 headings mappable to a chapter name
      (bank/broker/insurer annual-report formats — a generic structure on par
      with the standard form, not a per-company exception).
    Neither hit → return empty — the contract violation dispatches the Agent.
    A chapter anchor with an empty title (two-line form) takes the first
    heading line inside the chapter."""
    anchors = list(_SECTION_ANCHOR_RE.finditer(md))
    if len(anchors) >= _MIN_ANCHORS:
        out: List[Tuple[str, str, str]] = []
        for i, m in enumerate(anchors):
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(md)
            title = _clean_sec_title(m.group("title"))
            if not title:
                hm = re.search(r"^#{1,6}[ \t*`]*([^\n]+)$",
                               md[m.end():end][:400], re.M)
                if hm:
                    title = _clean_sec_title(hm.group(1))
            out.append((_section_key(title), title, md[m.end():end]))
        return out
    return _split_topic(md)


_TOPIC_HEAD_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>[^\n]+)$", re.M)


def _split_topic(md: str) -> List[Tuple[str, str, str]]:
    """Financial topic-style dialect: split chapters at the mappable heading
    level (all headings at that level bound the chapters; unmapped chapters
    are dropped)."""
    mapped: Dict[int, int] = {}
    for m in _TOPIC_HEAD_RE.finditer(md):
        title = m.group("title").strip()
        if title == "目录" or set(title) <= set("# *"):
            continue
        if _section_key(title) != "other":
            lvl = len(m.group("marks"))
            mapped[lvl] = mapped.get(lvl, 0) + 1
    lvl = next((l for l in sorted(mapped) if mapped[l] >= 3), None)
    if lvl is None:
        return []
    heads = [m for m in _TOPIC_HEAD_RE.finditer(md)
             if len(m.group("marks")) == lvl]
    out: List[Tuple[str, str, str]] = []
    for i, m in enumerate(heads):
        title = _clean_sec_title(m.group("title"))
        if not title or title == "目录":
            continue
        key = _section_key(title)
        if key == "other":
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        out.append((key, title, md[m.end():end]))
    return out


# ---------------- items: the two chapter-2 items (generic aliases + similarity + guards) ----------------

def extract_items(md: str, audit: Optional[Dict] = None) -> Dict[str, Dict]:
    """Full-text MD → {item_key: {title, text, tables}}. Misses are tolerated
    (missing keys go to the Agent)."""
    out: Dict[str, Dict] = {}
    headers = _header_lines(md)
    item_audit: Dict[str, Dict] = {}
    for item, aliases in ITEM_ALIASES.items():
        for m in re.finditer(r"^[^\n]{0,60}$", md, re.M):
            line = m.group(0)
            hit = _alias_hit(line, aliases)
            if hit is None:
                continue
            exact = hit in line
            rank = marker_rank(line)
            rank = rank if rank is not None else 2.0
            end = _block_end(md, m.end(), rank)
            block = md[m.end():end]
            # short-block guard: >=150 chars or contains a table (TOC
            # entries / header noise)
            if len(block.strip()) < 150 and "|" not in block:
                continue
            out[item] = _subsection_dict(_clean_title(line), block, headers)
            item_audit[item] = {
                "status": "exact" if exact else "fuzzy",
                "title": _clean_title(line),
                "score": 1.0 if exact else round(_sim(line, hit), 3)}
            break
        else:
            item_audit[item] = {"status": "miss"}
    if audit is not None:
        audit["items"] = item_audit
    return out


# ---------------- sections: subsection-anchor splitting (three forms) + rule modes ----------------

def extract_sections(md: str, audit: Optional[Dict] = None) -> Dict[str, Dict]:
    """Split chapters → subsections → {section_key: {title, subsections}}.
    Non-conforming structure (no subsection anchors / zero keyword hits) is
    skipped and recorded in audit — dispatched to the Agent."""
    headers = _header_lines(md)
    out: Dict[str, Dict] = {}
    seen: Dict[str, str] = {}
    for key, sec_title, text in split_sections(md):
        rule = SECTION_RULES.get(key)
        if not rule or not text.strip():
            continue
        if rule.get("skip_short") and len(text.strip()) < rule["skip_short"]:
            seen[key] = "skip_short"
            continue
        anchors = _subsection_anchors(text)
        l1 = min((a[2] for a in anchors), default=None)
        entries: List[Tuple[str, str]] = []
        if l1 is not None and rule.get("include_head"):
            head = text[:next(a[0] for a in anchors if a[2] == l1)]
            if head.strip():
                entries.append(("(节首前文)", head))
        for i, (a_start, a_end, a_rank) in enumerate(anchors):
            if a_rank != l1:
                continue
            title = _clean_title(text[a_start:a_end])
            if rule["mode"] == "keyword" and not any(
                    kw in title for kw in rule["keywords"]):
                continue
            entries.append((title, _render_body(text, anchors, i)))
        if not entries:
            seen[key] = "skip_no_keyword" if l1 is not None else "skip_no_anchors"
            continue
        sec = out.setdefault(key, {"title": _clean_title(sec_title),
                                   "subsections": []})
        for title, body in entries:
            sec["subsections"].append(_subsection_dict(title, body, headers))
        seen[key] = "ok"
    if audit is not None:
        audit["sections"] = {
            key: ({"status": "ok", "n": len(out[key]["subsections"])}
                  if key in out else {"status": seen.get(key, "miss"), "n": 0})
            for key in SECTION_RULES}
    return out


def extract(md: str, audit: Optional[Dict] = None) -> Tuple[Dict, Dict]:
    """L0 orchestration: MD → (items, sections)."""
    return extract_items(md, audit=audit), extract_sections(md, audit=audit)
