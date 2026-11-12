# -*- encoding: utf-8 -*-
"""
Periodic-report parsing: report PDF → MD → {items, sections} JSON (pure file
products, touches no database).

Covered categories (Q1/Q3/H1/AR):
- AR annual report (ndbg) / H1 semi-annual report (bndbg) — isomorphic
  parsing, the current parsing coverage; H1 chapters mirror AR (management
  discussion & analysis / risks), so H1 steps in when the AR is an
  image-only PDF without a text layer
- Q1 (yjdbg) / Q3 (sjdbg) — download and metadata already supported
  (client/downloader), parsing not yet covered (short reports lack the
  chapter-3-to-7 structure; separate rules needed, extend on demand later)

Pipeline:
  downloads/<ts_code>/<year>_<cat>.pdf (downloader product; file on disk = input)
    → MD       (pymupdf4llm main path; on failure/over-short output fall back
               to the text-layer chain fitz→pdfplumber→pypdf)
    → items    the two exact chapter-2 items (kpi3y/nonrecurring,
               vocab + similarity fallback)
    → sections chapters 3-7 in full by L1 subsection (SECTION_RULES): mdna in
               full / governance·matters by keywords / shareholders whole
               chapter + head preamble / bonds skipped when <500 chars;
               structurally non-conforming chapters are skipped and recorded
               in the audit
    → output/cninfo/md/<ts_code>/<year>_<cat>.md + _items.json
  Evidence extraction (called by research's annual_source): sections
    subsections → LLM (client injected by the caller) → four kinds of atomic
    facts operating/segment/statement/risk, each with quote/confidence/period
    in annual-report convention (migrated in 2026-10 from annual_source;
    formerly a standalone evidence.py, merged into this module 2026-10-11 as
    report handling)

Subsection splitting: the numbered form decides sibling rank (一、=2.5 /
N.M=3.0 / N.M.K=+0.1·segment / （一）=3.5); # heading depth is untrustworthy
— Montage's L1 and L2 are both ####; Industrial Bank's L1 mixes
####/#####/######. Chapter location is dual-dialect + hybrid (第X节/章
anchors >=4 / topic-style dynamic heading-level choice). Empirical basis for
dialects/cleaning/guards: host project
docs/agents/profile_extraction_handoff.md §5.

Input accepts security codes only (6-digit or ts_code); name resolution is
the caller's job (agent vocab). Storage note: current products are JSON
files; a structured-storage design is separate (do not add DB dependencies
here).

Usage:
    python -m tools.cninfo.parse 300308 [--year 2025] [--audit]
    python -m tools.cninfo.parse --index 000300.SH --year 2025   # index-constituent batch
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .downloader import DOWNLOAD_ROOT
from .filters import normalize_ts_code

logger = logging.getLogger(__name__)

MD_ROOT = Path("output/cninfo/md")

# ---------------- PDF → MD ----------------


def _extract_text(path: str) -> str:
    """Full-text extraction from the PDF text layer. Fallback chain:
    fitz → pdfplumber → pypdf; empty string if all fail. (Image-only/scanned
    PDFs have an empty text layer and need visual OCR — out of scope here.)"""
    try:
        import fitz
        with fitz.open(path) as doc:
            return "\n".join(p.get_text() for p in doc)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 fallback-chain catch-all for damaged files
        logger.warning(f"fitz 提取失败, 降级 pdfplumber: {e}")

    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pdfplumber 提取失败, 降级 pypdf: {e}")

    try:
        from pypdf import PdfReader
        with open(path, "rb") as f:
            reader = PdfReader(f)
            return "\n".join((p.extract_text() or "") for p in reader.pages)
    except ImportError:
        logger.warning("PDF 提取库均不可用 (需 pymupdf/pdfplumber/pypdf 之一)")
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PDF 文本提取全部失败 {path}: {e}")
        return ""


def to_markdown(path: str) -> str:
    """PDF → MD. pymupdf4llm main path (margins strip headers/footers); on
    failure/over-short output fall back to the plain text layer + page
    markers (keeps chapters locatable)."""
    try:
        import pymupdf4llm
        md = pymupdf4llm.to_markdown(doc=path, margins=(45, 45, 0, 0),
                                     write_images=False)
        if isinstance(md, str) and len(md.strip()) > 100:
            return md
        logger.warning(f"MD 转换结果过短, 走文本降级: {path}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MD 转换失败降级纯文本 {path}: {e}")

    text = _extract_text(path)
    if not text:
        return ""
    out, n = [], 0
    for i, line in enumerate(text.splitlines()):
        if i and i % 40 == 0:
            n += 1
            out.append(f"\n\n----- [第{n}页段] -----\n\n")
        out.append(line)
    return "".join(out)


# ---------------- ① location & ② boundaries ----------------

_L1_NUM = re.compile(r"^[一二三四五六七八九十]+、\s*\S{0,50}$")
_L2_NUM = re.compile(r"^[（(][一二三四五六七八九十]+[）)]\s*\S{0,50}$")
_D_NUM = re.compile(r"^\d+(?:\.\d+)+\s*\S{0,50}$")
_HEAD = re.compile(r"^(#{1,6})\s+\S")
_PAGE_NO = re.compile(r"^\d+\s*/\s*\d+$")

# The checkbox suffix '√适用□不适用' often sticks to the end of titles (seen
# in Wanhua 600309 / Chalco 601600; it once blew up the non-marker-line guard
# and caused misses); on its own line it is pure noise
_CHECKBOX = re.compile(r"[□√]\s*(?:不\s*)?适\s*用(?:\s*[□√]\s*(?:不\s*)?适\s*用)?")

_BARE_STRIP = re.compile(r"[#\s、。（(）)0-9.\-—第三节第四五六年度近<>*_/a-zA-Z]")


def marker_rank(line: str) -> Optional[float]:
    """Line marker rank (shallow→deep). Numbering beats # level — observed:
    Montage's L1 (六、) and L2 ((一)) are both ####, so # depth cannot tell
    siblings apart; the numbering kind must decide. Ranks: # heading = level;
    一、=2.5; N.M=3.0; N.M.K=3.0+0.1·extra segments (Industrial Bank's
    '4.1.6'/'4.1.6.1' once shared a rank with '4.1' and miscut the boundary);
    （一）=3.5. Strip ** bold first (Industrial Bank '##### **4.1.6**' case)."""
    line = _CHECKBOX.sub("", line.rstrip()).rstrip()   # strip trailing checkbox suffix
    body = re.sub(r"^#{1,6}\s*", "", line).replace("*", "")
    if _L1_NUM.match(body):
        return 2.5
    m = _D_NUM.match(body)
    if m:
        return 3.0 + 0.1 * (m.group(0).count(".") - 1)
    if _L2_NUM.match(body):
        return 3.5
    m = _HEAD.match(line)
    if m:
        return float(len(m.group(1)))
    return None


_CN_L1_LOOSE = re.compile(r"^[一二三四五六七八九十]+、\s*[^\n]{0,60}$")
_CN_L2_LOOSE = re.compile(r"^[（(][一二三四五六七八九十]+[）)]\s*[^\n]{0,60}$")
_NM_LOOSE = re.compile(r"^(\d+(?:\.\d+)+)\s*[^\n]{0,60}$")


def _anchor_rank(line: str) -> Optional[float]:
    """Subsection anchor rank (sections path): numbering forms only; a bare #
    heading does not count as an anchor.

    # -marked lines are highly trusted — titles loosen to 60 chars, spaces/**
    allowed (cases: Industrial Bank '#### 5.5 …受处 罚情况' with PDF
    line-wrap spaces; Montage's 78-char long title). Plain-text lines go
    through the strict marker_rank (guards against splitting narrative
    sentences; WuXi's unmarked plain-text title case)."""
    line = _CHECKBOX.sub("", line.rstrip()).rstrip()
    if _HEAD.match(line) is None:
        return marker_rank(line)
    body = re.sub(r"^#{1,6}\s*", "", line).replace("*", "")
    if _CN_L1_LOOSE.match(body):
        return 2.5
    if _CN_L2_LOOSE.match(body):
        return 3.5
    m = _NM_LOOSE.match(body)
    if m:
        return 3.0 + 0.1 * (m.group(1).count(".") - 1)
    return None


_TOC_LINE = re.compile(r"…|\.{4,}|\s\d+\s*$")

_SIM_STRIP = re.compile(r"[\s、，,。；;：:（）()\[\]【】/**#0-9.\-—·_`]")


def _sim(line: str, alias: str) -> float:
    """Normalized character similarity (difflib ratio). Compares after
    stripping whitespace/punctuation/digits/decoration —
    '非经常性损益项目及涉及金额' vs '非经常性损益项目和金额' ≈ 0.87."""
    a = _SIM_STRIP.sub("", line)
    b = _SIM_STRIP.sub("", alias)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _alias_hit(line: str, aliases: List[str]) -> Optional[str]:
    """Line hit against vocab aliases: exact containment → similarity fallback
    (marker lines >=0.75 / plain-text lines >=0.85). Guards: ① TOC entries
    excluded; ② coverage: bare >= 60% of the alias; ③ non-marker lines pass
    only when nearly a bare alias line (bare <= alias+3; the fallback channel
    is tightened to <=30) — excludes narrative sentences (Montage case:
    '报告期末公司前三年主要会计数据和财务指标如下…' once impersonated a
    title)."""
    line = _CHECKBOX.sub("", line).rstrip()
    for al in aliases:
        if al not in line:
            continue
        if _TOC_LINE.search(line):
            continue                     # TOC entry (dot leaders / trailing page number)
        bare = _BARE_STRIP.sub("", line)
        al_bare = re.sub(r"[和及与]", "", al)
        if len(bare) < 0.6 * len(al_bare):
            continue
        if marker_rank(line) is None and len(bare) > len(al_bare) + 3:
            continue
        return al
    # Similarity fallback: only lines inside the structural guards
    # participate; non-marker lines (plain text, low trust) get a tightened
    # threshold
    if _TOC_LINE.search(line):
        return None
    bare = _BARE_STRIP.sub("", line)
    is_marker = marker_rank(line) is not None
    if not is_marker and len(bare) > 30:
        return None
    best_al, best_r = None, 0.0
    for al in aliases:
        r = _sim(line, al)
        if r > best_r:
            best_al, best_r = al, r
    threshold = 0.75 if is_marker else 0.85
    return best_al if best_r >= threshold else None


def _block_end(md: str, from_pos: int, title_rank: float) -> int:
    """Content-block end = the next marker line with rank <= title rank
    (deeper sub-markers are content, not cut points)."""
    for m in re.finditer(r"^[^\n]{0,80}$", md[from_pos:], re.M):
        line = m.group(0)
        rank = marker_rank(line)
        if rank is not None and rank <= title_rank:
            return from_pos + m.start()
        if m.start() > 12000:
            return from_pos + m.start()
    return len(md)


# ---------------- chapter layer: generic-dialect splitting moved to
# ---------------- generic.py (L0 single dialect); topic-style/hybrid
# ---------------- exceptions → contract violation → repair Agent fallback


# ---------------- subsection layer: numbered-anchor splitting (§5.2) ----------------

_PUNCT_END = ("。", "；", "，", "：", ":", "！", "？")


def _subsection_anchors(text: str) -> List[Tuple[int, int, float]]:
    """Scan numbered anchors inside a chapter. Returns [(anchor start, anchor
    end, anchor rank)] in order.

    Plain-text anchor guard (WuXi case: '十四、募集资金…' left unmarked by
    pymupdf4llm): <=50 chars and not ending in sentence punctuation —
    narrative paragraphs (long lines / with a period) never hit. The #
    -marked anchor guard is loosened to 120 chars (Montage's 78-char long
    title case)."""
    out = []
    for m in re.finditer(r"^[^\n]{1,120}$", text, re.M):
        line = m.group(0)
        rank = _anchor_rank(line)
        if rank is None:
            continue
        if _HEAD.match(line) is None:
            s = line.strip()
            if len(s) > 50 or s.endswith(_PUNCT_END):
                continue
        out.append((m.start(), m.end(), rank))
    return out


def _render_body(section_text: str,
                 anchors: List[Tuple[int, int, float]], i: int) -> str:
    """The L1 body of anchors[i] = its direct text + the deeper sub-blocks
    that follow; sub-block titles are kept as [title] lines (cleaning
    directly would strip marker-line titles — Montage case: the (一)(二)
    subsection titles under '五、主要经营情况'). End = the next anchor with
    rank <= this rank (a bare # heading does not count as a boundary — in
    Industrial Bank's chapter 1, '### 公司组织架构图' and '#### 1.1' share a
    visual level; splitting by # level once lost content)."""
    _, a_end, rank = anchors[i]
    end = len(section_text)
    for j in range(i + 1, len(anchors)):
        if anchors[j][2] <= rank:
            end = anchors[j][0]
            break
    parts, pos = [], a_end
    for j in range(i + 1, len(anchors)):
        s, e, _r = anchors[j]
        if s >= end:
            break
        parts.append(section_text[pos:s])
        parts.append(f"\n[{_clean_title(section_text[s:e])}]\n")
        pos = e
    parts.append(section_text[pos:end])
    return "".join(parts)


# ---------------- ③ separation & ④ cleaning ----------------

_ROW_RE = re.compile(r"^\|.+\|\s*$")


def _clean_cell(cell: str) -> str:
    """Cell cleaning: <br> is a line-wrap artifact; removing it lets words
    rejoin across lines."""
    return cell.strip().strip("|").replace("<br>", "").strip()


def parse_md_tables(text: str) -> List[Dict]:
    """Parse MD pipe tables. Multiple separator rows split into multiple
    tables; embedded caliber rows (e.g. '||2025年末|…') are kept as data
    rows."""
    tables: List[Dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not _ROW_RE.match(lines[i]):
            i += 1
            continue
        block = []
        while i < len(lines) and _ROW_RE.match(lines[i]):
            block.append(lines[i])
            i += 1
        cells = [[_clean_cell(c) for c in row.split("|")[1:-1]] for row in block]
        # Degenerate pipe block without a separator row (pymupdf4llm output
        # for header-less tables; China Merchants Bank case
        # '|**合计**<br>179|392|'): start with an empty header and take the
        # whole block as data rows instead of losing it entirely
        has_sep = any(row and all(set(c) <= set("-: ") and c for c in row)
                      for row in cells)
        current: Optional[Dict] = None if has_sep else {"header": [], "rows": []}
        for j, row in enumerate(cells):
            is_sep = row and all(set(c) <= set("-: ") and c for c in row)
            if is_sep:
                if current and current["rows"]:
                    tables.append(current)
                current = {"header": cells[j - 1] if j else [], "rows": []}
            elif current is not None:
                if any(c for c in row):
                    current["rows"].append(row)
        if current and current["rows"]:
            tables.append(current)
    return tables


def _clean_title(line: str) -> str:
    """Title cleaning: drop #/decorations (<u> ** etc.)/numbering/checkboxes,
    keep plain text. Strip decorations first — case: in '<u>(3)</u> …' the
    numbering sits inside the decoration."""
    t = _CHECKBOX.sub("", line)
    t = re.sub(r"^#{1,6}\s*", "", t)
    t = re.sub(r"<[^>]+>|[*_`]+", "", t)
    t = re.sub(r"^[（(]?(?:[一二三四五六七八九十]+|\d+)[）)]?[、．.]?\s*", "", t)
    t = re.sub(r"^\d+(?:\.\d+)*\s*", "", t)
    return t.strip()


def _header_lines(md: str) -> set:
    """Document-level header set: short name-like lines appearing >=10 times
    (company name/report name/column name repeated on every page)."""
    cnt = Counter(l.strip() for l in md.splitlines()
                  if l.strip() and len(l.strip()) <= 30)
    return {l for l, n in cnt.items()
            if n >= 10 and not l[-1] in "。；，：！？" and not l.startswith("|")}


_BARE_PAGE = re.compile(r"^\d{1,4}$")


def _clean_text(block: str, headers: set = frozenset()) -> str:
    """Narrative text: strip table rows / page-number rows ('N / M' and
    Industrial Bank's bare digits '15') / title marker lines / checkboxes /
    repeated headers / HTML decorations. Dual header method: document-level
    set (>=10 short lines) + in-block frequency (>=2 times; case
    '澜起科技股份有限公司')."""
    lines_ = []
    for line in block.splitlines():
        line = re.sub(r"<[^>]+>|[*_]+", "", line.rstrip())   # strip decorations first
        line = _CHECKBOX.sub("", line)              # checkbox line/suffix (a now-empty line gets filtered)
        s = line.strip()
        if _ROW_RE.match(line) or _PAGE_NO.match(s) or _BARE_PAGE.match(s):
            continue
        if marker_rank(line) is not None:      # title marker lines are structure, not content
            continue
        if s and s not in headers:
            lines_.append(s)
    # Repeated header: a short name-like line (<=30 chars, no sentence
    # punctuation) appearing >=2 times is a header (case: '澜起科技股份有限公司')
    freq = Counter(lines_)
    return "\n".join(
        l for l in lines_
        if freq[l] < 2 or len(l) > 30 or l[-1] in "。；，：！？")


def _clean_tables(tables: List[Dict], title: str) -> List[Dict]:
    """Table cleaning: if the first row is a unit row like '单位：元' → fold
    it into the table's unit field and drop the row."""
    for t in tables:
        h = t.get("header") or []
        if any("单位" in c for c in h) and sum(1 for c in h if c.strip()) <= 2:
            m = re.search(r"单位[:：]\s*([^\s|]+)", "".join(h))
            t["unit"] = m.group(1) if m else ""
            t["header"] = []
        t["name"] = title
    return tables


def _subsection_dict(title: str, body: str, headers: set) -> Dict:
    """Subsection triple {title, text, tables}: reuses the items cleaning
    pipeline."""
    tables = _clean_tables(parse_md_tables(body), title)
    um = re.search(r"^\s*单位[:：]\s*([^\s|]+)", body, re.M)
    if um and tables and not tables[0].get("unit"):
        tables[0]["unit"] = um.group(1)   # standalone unit line before the table
    return {"title": title, "text": _clean_text(body, headers),
            "tables": tables}


# ---------------- disclosure-item vocab & section rules ----------------

# Chapter-2 disclosure-item vocab (controlled item_keys; aliases grow with
# each observed wording — add one whenever new phrasing appears). Chapters
# 3-7 no longer go through vocab — the sections structure takes subsections
# in full for more complete coverage.
ITEM_ALIASES: Dict[str, List[str]] = {
    "kpi3y": ["主要会计数据和财务指标", "主要财务数据及指标", "近三年主要会计数据",
              "近三年主要财务数据", "会计数据和财务指标摘要", "主要会计数据及财务指标",
              "财务概要", "财务摘要"],
    # Note: the short alias 「主要财务指标」 is deliberately not listed — it
    # would anchor kpi3y to the chapter-2 section heading (Montage case:
    # '## 第二节 公司简介和主要财务指标'); that wording is covered by the
    # similarity fallback
    "nonrecurring": ["非经常性损益项目和金额", "非经常性损益项目及涉及金额",
                     "非经常性损益项目", "非经常性损益明细表", "非经常性损益表"],
}

# Chapters 3-7 rules (§3, user decision)
SECTION_RULES: Dict[str, Dict] = {
    "mdna":         {"mode": "all"},          # take everything (user's words: "it is all important")
    "governance":   {"mode": "keyword",
                     "keywords": ["董监高", "董事和高级管理人员", "员工",
                                  "股权激励", "审计委员会"]},
    "matters":      {"mode": "keyword",
                     "keywords": ["募集资金", "募投", "承诺", "关联交易",
                                  "担保", "诉讼", "退市", "非标"]},
    "shareholders": {"mode": "all", "include_head": True},   # head preamble = total-shareholders field
    "bonds":        {"mode": "all", "skip_short": 500},      # "not applicable" for most companies
}


# ---------------- orchestration ----------------

def _locate_report_pdfs(ts_code: str,
                        year: Optional[int] = None) -> list:
    """AR+H1 candidates (report period new→old, same-period AR preferred).
    The semi-annual report is isomorphic with the annual one (management
    discussion & analysis / risk chapters); when the AR is an image-only PDF
    without a text layer, the H1 steps in.

    The download directory is the source of truth (downloader product):
    downloads/<ts_code>/<year>_<cat>.pdf — file on disk means downloaded;
    sorted year DESC, category DESC ('ndbg' > 'bndbg', same-period AR
    first)."""
    root = DOWNLOAD_ROOT / ts_code
    if not root.is_dir():
        return []
    cands = list(root.glob("*_ndbg.pdf")) + list(root.glob("*_bndbg.pdf"))
    out = [{"report_year": int(p.stem.split("_")[0]),
            "category": p.stem.split("_", 1)[1], "file_path": str(p)}
           for p in cands]
    out = [c for c in out if year is None or c["report_year"] == year]
    out.sort(key=lambda c: (c["report_year"], c["category"]), reverse=True)
    return out


def build_items(stock: str, year: Optional[int] = None,
                want_audit: bool = False) -> Dict:
    """Single stock: locate the report PDF (annual→semi-annual fallback
    chain) → MD → items+sections → JSON written to disk.

    Input accepts security codes only (name resolution is the caller's
    job)."""
    ts_code = normalize_ts_code(stock.strip())
    if not ts_code:
        return {"ok": False, "error": f"未识别证券代码: {stock} "
                f"(只认 6 位代码或 ts_code, 名称解析归调用方)"}

    cands = _locate_report_pdfs(ts_code, year)
    if not cands:
        return {"ok": False, "error":
                f"{ts_code} 无已下载年报/半年报 PDF (先跑 tools.cninfo.downloader)"}

    loc, md = None, ""
    for row in cands:
        if not Path(row["file_path"]).exists():
            continue
        md = to_markdown(row["file_path"])
        if len(md.strip()) >= 2000:
            loc = row
            break
    if loc is None:
        return {"ok": False, "error": f"{ts_code} 全部候选报告无文本层"}

    out_dir = MD_ROOT / ts_code
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{loc['report_year']}_{loc['category']}.md").write_text(
        md, encoding="utf-8")

    audit: Dict = {}
    # L0 generic processing (single dialect) — exceptional violations fall
    # to the repair Agent
    from .generic import extract as _generic_extract
    items, sections = _generic_extract(md, audit=audit if want_audit else None)
    json_path = out_dir / f"{loc['report_year']}_{loc['category']}_items.json"
    json_path.write_text(
        json.dumps({"ts_code": ts_code, "report_year": loc["report_year"],
                    "category": loc["category"], "items": items,
                    "sections": sections},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    sec_stat = {k: len(v["subsections"]) for k, v in sections.items()}
    logger.info(f"披露项提取: {ts_code} {loc['report_year']} "
                f"items {list(items)} + sections {sec_stat} → {json_path}")
    r = {"ok": True, "ts_code": ts_code, "report_year": loc["report_year"],
         "items": list(items), "sections": sec_stat, "json": str(json_path)}
    if want_audit:
        r["audit"] = audit
    return r


def build_for_index(index_code: str, year: Optional[int] = None) -> Dict[str, int]:
    """Index-constituent batch → items.json per stock (latest tushare
    constituents, re-extracted stock by stock)."""
    from .downloader import _load_index_universe
    codes = [s["ts_code"] for s in _load_index_universe(index_code)]
    logger.info(f"披露项批量: {index_code} {len(codes)} 只")
    stats = {"ok": 0, "err": 0}
    for i, code in enumerate(codes, 1):
        r = build_items(code, year=year)
        stats["ok" if r["ok"] else "err"] += 1
        if not r["ok"]:
            logger.warning(f"[{i}/{len(codes)}] {code} 失败: {r['error']}")
        else:
            logger.info(f"[{i}/{len(codes)}] {code} {len(r['items'])}项 "
                        f"{r.get('sections', {})}")
        time.sleep(1)
    return stats


def annual_corpus(ts_code: str, year: Optional[int] = None,
                  max_chars: int = 2500) -> Optional[str]:
    """items+sections → annual-report highlights corpus (deterministic, zero
    LLM).

    Corpus = kpi3y (indicator notes) + the mdna operations-discussion
    subsection + the mdna risk subsection, texts merged, each part truncated
    to max_chars; if items.json does not exist, build_items runs on the spot.
    Companies whose sections were skipped for non-conforming structure
    (dialect B / ESG-style governance) fall back to kpi3y or legacy keys;
    all-empty returns None (the caller degrades itself, e.g. the writing
    side falls back to the quant profile)."""
    if not ts_code:
        return None
    paths = sorted(MD_ROOT.glob(f"{ts_code}/*_items.json"), reverse=True)
    if year:
        paths = [p for p in paths if p.name.startswith(f"{year}_")] or paths
    if not paths:
        r = build_items(ts_code)
        if not r.get("ok"):
            return None
        paths = [Path(r["json"])]
    d = json.loads(paths[0].read_text(encoding="utf-8"))
    items = d.get("items", {})
    sections = d.get("sections", {})
    label = (f"{d['report_year']} 年度报告" if d.get("category") == "ndbg"
             else f"{d['report_year']} 半年度报告")
    parts = []
    kpi = items.get("kpi3y")
    if kpi and kpi.get("text"):
        parts.append(f"### 主要会计数据与指标说明\n{kpi['text'][:max_chars]}")
    mdna_subs = sections.get("mdna", {}).get("subsections", [])
    picks = (("经营情况讨论(管理层口径)",
              ("经营情况讨论", "主要经营情况", "经营情况分析", "业务情况分析")),
             ("公司自认风险", ("风险",)))
    for head, kws in picks:
        block = next((s["text"] for s in mdna_subs
                      if s.get("text") and any(kw in s["title"] for kw in kws)),
                     None)
        if block is None and head.startswith("经营"):
            # wording-drift fallback for banks etc.: take the first non-empty
            # mdna subsection (Industrial Bank case: '十四五成就回顾')
            block = next((s["text"] for s in mdna_subs if s.get("text")), None)
        if block:
            parts.append(f"### {head}\n{block[:max_chars]}")
    if not parts:
        # legacy items.json compatibility (business_review/risks vocab keys)
        for key, head in (("business_review", "经营情况讨论(管理层口径)"),
                          ("risks", "公司自认风险")):
            it = items.get(key)
            if it and it.get("text"):
                parts.append(f"### {head}\n{it['text'][:max_chars]}")
    if not parts:
        return None
    return f"### {label}要点 (年报披露项提取)\n" + "\n\n".join(parts)


# ---------------- evidence extraction: annual-report atomic facts (LLM client injected by the caller) ----------------
# Migrated in 2026-10 from skills/research/sources/annual_source.py (formerly
# a standalone evidence.py; merged into this module 2026-10-11 as report
# handling). Orchestration and the EvidenceShard contract (eid generation /
# assembly / contract fallback / spot-check list / persistence) live in
# annual_source.py on the research side. Four atomic kinds: operating =
# operating data / segment = segment detail / statement = management claim /
# risk = risk item; period in annual-report convention: flows "<year>A" /
# period-end stocks "<year>-12-31".

SECTION_ORDER = ("mdna", "governance", "matters", "shareholders")
SECTION_CN = {
    "mdna": "管理层讨论与分析",
    "governance": "公司治理",
    "matters": "重要事项",
    "shareholders": "股份变动及股东情况",
}
CATEGORIES = ("operating", "segment", "statement", "risk")
CATEGORY_CN = {
    "operating": "经营数据",
    "segment": "分部明细",
    "statement": "管理层论断",
    "risk": "风险条目",
}
# Empty-subsection threshold: a body too short with no table is not worth an
# LLM call
MIN_BODY_CHARS = 30
# Contract fallback: when content contains a digit but value/unit are both
# empty, rescue the unit immediately following the number (longest word
# first; it must be adjacent to the digit — guards against mismatches like
# "发达国【家】" / "两【个】方向")
_UNIT_ADJACENT_RE = re.compile(
    r"\d(?:,\d{3})*(?:\.\d+)?\s*(个百分点|万吨|千吨|亿元|万元|亿|万|吨|%|元|家|人|个|项|次|名|强|款|瓶|户|亩|度|千瓦时)"
)
_HAS_DIGIT = re.compile(r"\d")

# Legal period formats (mirrored from the host research contract
# contracts.valid_period; this module stays self-contained):
# YYYY / YYYYA / YYYYE / YYYYH1 / YYYYQn / YYYY-MM-DD (a real date)
_PERIOD_RE = re.compile(
    r"^(?:\d{4}|\d{4}A|\d{4}E|\d{4}H1|\d{4}Q[1-4]"
    r"|\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))$"
)

# The system prompt is a plain string (not a .format/f-string template);
# JSON braces are written literally
_SYSTEM_PROMPT = """你是年报细粒度证据抽取器。任务: 从用户给出的年报小节原文中抽取原子级证据事实, 供研究报告显式引用。

【抽取纪律 — 违反任一条即为废稿】
1. 只抽取原文明确陈述的信息; 严禁推断、演绎、跨句拼接、用常识补全。
2. 数字原样照抄: content 与 quote 中的数值保留原文写法 (含千分位逗号与小数位); value 字段才是去千分位的纯数值。
3. 每条必须附 quote = 原句逐字摘录 (表格数据摘抄整行, 单元格以" | "连接; 长句截取包含该数字的最短完整子句), 供人工复核对照。
4. content 为单句且自含完整信息: 指标名 + 数值 + 单位 + 期间; 不用"公司/本公司"等代词开头。
5. 拿不准的一律不抽。
6. 所有文本字段一律简体中文 (原文如此则照抄)。
7. 数量纪律: 单次输出不超过 60 条, 优先级 operating > segment > risk > statement; 超出时舍弃低价值条目。
8. statement 与 risk 类: content 中不要出现具体数字 (期间信息放 period 字段);
   论断本身的关键数字须原文直引, 并同时填 value 与 unit。

【四类原子 (category)】
- operating 经营数据: 产能/产量/销量/库存量/利用率/单价/经销商数量/门店数/员工与研发人员/在建项目/原料采购/分红回购 等经营指标 (财务报表之外的数字证据, 优先级最高)。
- segment 分部明细: 分产品/分档次/分地区/分渠道/分行业的收入、成本、毛利率及同比变动。
- statement 管理层论断: 展望、战略、经营举措、风险应对的原句要点引用 (是公司管理层观点, 不得虚构或推算数字)。
- risk 风险条目: 公司披露的风险因素, 逐条原子化。

【输出格式 — 只输出一个 JSON 对象, 围栏与解释都不要】
{"facts": [{"category": "operating", "content": "贵州茅台2025年度酒类产量116,123.73吨", "value": 116123.73, "unit": "吨", "period": "2025A", "quote": "酒类 | 吨 | 116,123.73 | 85,104.14 | 339,977.86 | 11.25 | 2.13 | 9.67", "confidence": 1.0, "formula": ""}]}

【字段规则】
- category: operating / segment / statement / risk 四选一。
- value: 数值型证据的纯数值 (去千分位与百分号; 百分数填数字本身, 如 16.50% 填 16.50); 非数值型填 null。
- unit: 原始单位 (吨/万吨/万元/亿元/元/%/个百分点/家/人/个/项等); 表注单位 (如"单位:万元") 必须并入 unit 或 content, 不得丢失; 非数值型填 ""。
- period: 报告期流量数据填 "<年>A"; 期末时点存量 (库存量/经销商数/期末员工数等) 填 "<年>-12-31" (年份见用户消息)。
- confidence: 原文直引填 1.0; 经换算/推算 (如由同比反推) 填 0.9 且 formula 写明公式; 直引时 formula 为 ""。"""


def _table_to_lines(table: Dict[str, Any]) -> List[str]:
    """Table → pipe-separated text lines (table name/unit folded into the
    first line so the LLM keeps the unit caliber)."""
    if not isinstance(table, dict):
        return []
    lines: List[str] = []
    head = " ".join(x for x in (table.get("name") or "", table.get("unit") or "") if x)
    if head:
        lines.append(f"[{head}]")
    header = [str(h) for h in (table.get("header") or []) if str(h).strip()]
    if header:
        lines.append("| " + " | ".join(header))
    for row in table.get("rows") or []:
        cells = [str(c) for c in row]
        if any(c.strip() for c in cells):
            lines.append("| " + " | ".join(cells))
    return lines


def build_subsection_body(sub: Dict[str, Any]) -> str:
    """Subsection body + tables → the source block fed to the LLM."""
    parts: List[str] = []
    text = (sub.get("text") or "").strip()
    if text:
        parts.append(text)
    table_lines: List[str] = []
    for t in sub.get("tables") or []:
        table_lines.extend(_table_to_lines(t))
    if table_lines:
        parts.append("【表格】(管道符分隔; 表名行的方括号内为表名与单位)")
        parts.extend(table_lines)
    return "\n".join(parts)


def extract_subsection(llm, year: int, sec_key: str, title: str,
                       body: str) -> List[Dict[str, Any]]:
    """One LLM call per subsection → raw atomic array (uncleaned); raises
    ValueError on parse failure.

    llm is injected by the caller (any client with
    .invoke([{role,content}...]) -> str suffices).
    """
    user = (
        f"小节定位: {year}年年度报告 · {SECTION_CN.get(sec_key, sec_key)} ({sec_key}) · 小节「{title}」\n"
        f"period 取值: 报告期流量用 \"{year}A\"; 期末时点用 \"{year}-12-31\"。\n\n"
        f"【原文】\n{body}"
    )
    resp = llm.invoke([
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ])
    facts = _parse_llm_facts(resp)
    if facts is None:
        raise ValueError(f"LLM 输出无法解析为 JSON: {resp[:200]!r}")
    return facts


def _extract_outer_json(t: str) -> Any:
    """Peel the outermost JSON container out of mixed text: start at the
    first [ or {, take through the last matching closing bracket."""
    order = sorted((p, o, c) for p, o, c in
                   ((t.find("["), "[", "]"), (t.find("{"), "{", "}")) if p >= 0)
    for start, _, close in order:
        frag = t[start:t.rfind(close) + 1]
        if frag:
            try:
                return json.loads(frag)
            except json.JSONDecodeError:
                continue
    return None


def _parse_llm_facts(text: str) -> Optional[List[Dict[str, Any]]]:
    """Lenient parsing: strip ```json fences; accept {"facts":[...]} wrappers
    or a bare array; None on total failure."""
    if not text or not text.strip():
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        obj = _extract_outer_json(t)
    if isinstance(obj, dict):
        obj = obj.get("facts") or obj.get("data") or []
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    return None


def _to_float(v: Any) -> Optional[float]:
    """Lenient value coercion: thousands separators/percent signs/
    unit-bearing strings → plain float; None if not coercible."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("，", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _valid_period(period: str) -> bool:
    """Legal period format (same rules as the host research contract; the
    date form also rejects 02-30)."""
    if not _PERIOD_RE.match(period):
        return False
    if len(period) == 10:
        try:
            datetime.strptime(period, "%Y-%m-%d")
        except ValueError:
            return False
    return True


def _norm_period(p: Any, year: int) -> str:
    """Period normalization: the annual-report convention allows only
    {year}A (flows) / {year}-12-31 (period-end stocks) / a legal original
    value."""
    s = str(p or "").strip()
    flow = ("", str(year), f"{year}年", f"{year}年度", f"{year}A", "报告期", "本期")
    stock = (f"{year}年末", f"{year}-12-31", "期末", f"{year}年期末")
    if s in flow:
        return f"{year}A"
    if s in stock:
        return f"{year}-12-31"
    return s if _valid_period(s) else f"{year}A"


def sanitize_fact(raw: Dict[str, Any], year: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """LLM raw entry → clean entry; returns (entry, drop reason) — an entry
    of None means dropped."""
    category = str(raw.get("category") or "").strip()
    if category not in CATEGORIES:
        return None, "bad_category"
    content = re.sub(r"\s+", " ", str(raw.get("content") or "")).strip()
    if not content:
        return None, "empty_content"
    value = _to_float(raw.get("value"))
    unit = str(raw.get("unit") or "").strip()
    if value is None and not unit and _HAS_DIGIT.search(content):
        m = _UNIT_ADJACENT_RE.search(content)      # contract fallback: rescue the unit after the digit
        if not m:
            return None, "numeric_without_value_unit"
        unit = m.group(1)
    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence", 1.0))))
    except (TypeError, ValueError):
        confidence = 1.0
    return {
        "category": category,
        "content": content,
        "value": value,
        "unit": unit,
        "period": _norm_period(raw.get("period"), year),
        "quote": str(raw.get("quote") or "").replace("\n", " ").strip(),
        "confidence": confidence,
        "formula": str(raw.get("formula") or "").strip(),
    }, ""


def main() -> int:
    p = argparse.ArgumentParser(description="定期报告披露项提取 (JSON 文件产物)")
    p.add_argument("stock", nargs="?", default="", help="证券代码 (6位或 ts_code)")
    p.add_argument("--year", type=int, default=0, help="报告年度 (缺省最新)")
    p.add_argument("--audit", action="store_true",
                   help="输出审计 (命中方式/相似度/跳过原因)")
    p.add_argument("--index", default="",
                   help="指数成分批量 (如 000300.SH, 需 TUSHARE_TOKEN)")
    args = p.parse_args()

    if args.index:
        stats = build_for_index(args.index, year=args.year or None)
        print(f"[INDEX] {stats}")
        return 0

    if not args.stock:
        p.error("需提供证券代码或 --index")
    r = build_items(args.stock, year=args.year or None, want_audit=args.audit)
    if not r["ok"]:
        print(f"[ERR] {r['error']}")
        return 1
    print(json.dumps(r, ensure_ascii=False, indent=1,
                     default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
