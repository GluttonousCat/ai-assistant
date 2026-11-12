# -*- encoding: utf-8 -*-
"""T06 draft generator — Stage 2 core: evidence pack -> ten-chapter draft + hypotheses
list + citation-check flags.

Flow (docs/research/tasks/T06_草稿生成器与引用校验.md):
    load_pack reads the evidence shards -> per chapter over T05 CHAPTERS:
      route/filter the evidence subset by wants -> digest (one line per item, <=80 items,
      truncated past 30k chars with a flag)
      -> get_agent_llm().invoke non-streaming generation (1 retry on failure; skip with
      a flag if it still fails)
      -> extract inline body citations back into used_eids
    The hypotheses JSON is requested by the T05 template appended to the hypotheses
    chapter's prompt (HYPOTHESES_CHAPTER_NO=8, scenario analysis; the task doc's "第10章"
    is a chapter-number typo — the T05 committed constant wins); the generator leniently
    parses every chapter's output, and on hitting a "statement array" strips it from the
    body and backfills Draft.hypotheses
    -> citation_check whole-draft validation + T01 validate_draft contract validation
    -> save_draft.

CLI:
    python -m skills.research.compose.generator 贵州茅台
    python -m skills.research.compose.generator 600519.SH --years 5

The prompt library is imported directly from T05 (prompts_chapters.CHAPTERS/
chapter_prompt/HYPOTHESES_CHAPTER_NO; chapter_prompt already embeds STYLE_PROMPT).
Tests and offline scenarios inject a stub via the llm= parameter, never a real model.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger
from core.paths import PathManager
from skills.research.compose.citation_check import check_draft, extract_citations
from skills.research.contracts import (
    Chapter,
    Draft,
    Fact,
    Hypothesis,
    View,
    load_pack,
    load_shard,
    research_dir as default_research_dir,
    save_draft,
    validate_draft,
)
from skills.research.prompts_chapters import (
    CHAPTERS,
    HYPOTHESES_CHAPTER_NO,
    chapter_prompt,
)

logger = get_logger(__name__)

# ---------- digest budget (task spec: one line per item, <=80 items; truncate past 30k chars) ----------
TOTAL_ITEM_CAP = 80        # per-chapter cap on total evidence lines
FACT_ITEM_CAP = 60         # facts' share when facts/views coexist (views keep a floor of 20, never squeezed out)
VIEW_ITEM_CAP = 20
DIGEST_CHAR_CAP = 30000

RETRY_TIMES = 2                     # 1 retry for a failed chapter (2 attempts total)

_TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)
_JSON_ARRAY_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.S)
_FENCE_WRAP_RE = re.compile(r"^```[a-zA-Z]*[ \t]*\n(.*?)\n?```\s*$", re.S)

# Fallback instruction for the hypotheses chapter — appended only when the template
# prompt itself lacks the hypotheses-JSON request
# (note: this module does not assemble this string with .format; JSON braces need no escaping)
_HYPOTHESES_INSTRUCTION = """
## 附加要求: 假设清单 (本章独有)
正文完成后, 另起一段用 ```json 围栏输出 3~6 条研究假设, 严格按下述结构 (数组):
[{"statement": "可证伪的假设陈述 (一句)",
  "indicators": ["验证指标 (写明看什么数据/什么条件算验证或证伪)"],
  "related_eids": ["支撑该假设的证据编号, 只用上文证据摘要中出现过的 eid/vid"]}]
围栏外不要再输出任何解释。"""

_RETRY_NUDGE = "\n\n(注意: 上次调用失败或输出为空, 请严格按输出约定重新生成本章正文。)"


# ---------- stock / directory resolution ----------

def _shard_info(d: Path) -> Optional[Tuple[str, str]]:
    """Read the first legal shard in the evidence directory -> (ts_code, name); None when no shard exists."""
    for p in sorted(d.glob("*.json")):
        try:
            shard = load_shard(p)
        except Exception:
            continue
        if not getattr(shard, "kind", ""):
            continue
        name = str(shard.meta.get("name") or shard.meta.get("stock")
                   or shard.meta.get("stock_name") or "")
        return (shard.ts_code or d.name), name
    return None


def resolve_stock(stock: str, root: Path,
                  rdir: Optional[Path] = None) -> Tuple[str, str, Path]:
    """Resolve "stock name or code" into (ts_code, name, evidence directory).

    A code (600519.SH) locates directly; a name is matched against existing evidence
    directories' shard meta (Stage 1 shard meta records the stock name); an explicitly
    given non-empty directory is trusted; finally falls back to "the sole evidence
    directory"; on total failure, hints at passing the code directly.
    """
    stock = (stock or "").strip()
    if not stock:
        raise ValueError("stock 不能为空")
    if _TS_CODE_RE.match(stock):
        ts = stock.upper()
        d = rdir or default_research_dir(root, ts)
        info = _shard_info(d) if d.is_dir() else None
        return ts, (info[1] if info and info[1] else ts), d

    base = root / "research"
    candidates = [rdir] if rdir is not None else (
        sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else [])
    infos = {d: _shard_info(d) for d in candidates if d.is_dir()}
    for d, info in infos.items():
        if info and info[1] == stock:
            return info[0], info[1], d
    if rdir is not None:
        info = infos.get(rdir)
        if info:
            return info[0], info[1] or stock, rdir
    non_empty = [d for d, info in infos.items() if info]
    if rdir is None and len(non_empty) == 1:
        d = non_empty[0]
        return infos[d][0], infos[d][1] or stock, d
    raise ValueError(f"无法把 {stock!r} 解析为 ts_code: 无匹配证据目录, "
                     f"请直接传 6 位代码 (如 600519.SH) 或 --dir 指定证据目录")


# ---------- evidence routing and digesting ----------

_VIEW_WANTS = {"views", "view", "reports", "report"}

# Matching rules for the T05 EVIDENCE_CATEGORIES closed set — eid prefixes (mainbiz ⊂ profile; listing both is harmless)
_WANT_PREFIX_RULES = {
    "profile": ("T:profile.",),
    "mainbiz": ("T:profile.mainbiz.",),
    "financials": ("T:tushare.",),
    "tushare": ("T:tushare.",),            # historical alias of financials
    "annual": ("A:",),
}


def _select_for_want(want: str, facts: List[Fact],
                     views: List[View]) -> Tuple[List[Fact], List[View]]:
    """Route by evidence category (T05 EVIDENCE_CATEGORIES closed set + task-spec aliases).

    views/reports -> all views; the rest match on eid prefixes (profile/mainbiz/
    financials/annual, rules taken from the T05 committed template vocabulary); a want
    outside the closed set degrades to T:-domain / metric-token matching.
    """
    w = want.strip().lower()
    if w in _VIEW_WANTS:
        return [], list(views)
    prefixes = _WANT_PREFIX_RULES.get(w)
    if prefixes:
        return [f for f in facts if f.eid.startswith(prefixes)], []
    return [f for f in facts
            if f.eid.startswith("T:") and w in f.eid[2:].split(".")], []


def select_chapter_evidence(wants: List[str], facts: List[Fact],
                            views: List[View]) -> Tuple[List[Fact], List[View], List[str]]:
    """Aggregate the evidence subset needed by one chapter (dedup across categories,
    order preserved); falls back to the full pack with a flag when nothing matches."""
    sel_f: List[Fact] = []
    sel_v: List[View] = []
    seen_e: set = set()
    seen_v: set = set()
    for w in wants or []:
        sf, sv = _select_for_want(w, facts, views)
        for f in sf:
            if f.eid not in seen_e:
                seen_e.add(f.eid)
                sel_f.append(f)
        for v in sv:
            if v.vid not in seen_v:
                seen_v.add(v.vid)
                sel_v.append(v)
    flags: List[str] = []
    if not sel_f and not sel_v and (facts or views):
        flags.append(f"evidence-route-fallback: wants={wants} 未命中任何证据, 已回退全量证据")
        return list(facts), list(views), flags
    return sel_f, sel_v, flags


def _period_year(period: str) -> Optional[int]:
    m = re.match(r"((?:19|20)\d{2})", (period or "").strip())
    return int(m.group(1)) if m else None


def _fact_year(f: Fact) -> Optional[int]:
    return _period_year(f.period)


def _view_year(v: View) -> Optional[int]:
    return _period_year(v.period) or _period_year(v.publish_date)


def build_digest(ch_no: int, facts: List[Fact], views: List[View],
                 char_cap: int = DIGEST_CHAR_CAP) -> Tuple[str, str, List[str]]:
    """Evidence subset -> (facts_digest, views_digest, flags): line-by-line digest, newest periods first.

    Line format: fact `- [E:eid] content｜来源: source｜期间`; view `- [V:vid] org(date): statement`.
    Item cap 80: 60 facts + 20 views when both kinds coexist (views never squeezed out),
    80 for a lone kind; when the two digests together exceed char_cap, the fact digest is
    trimmed first (keeping views intact), with a flag.
    """
    flags: List[str] = []
    fs = sorted(facts, key=lambda f: -(_fact_year(f) or 0))       # stable sort: same-year keeps pack order
    vs = sorted(views, key=lambda v: -(_view_year(v) or 0))
    fact_cap = FACT_ITEM_CAP if vs else TOTAL_ITEM_CAP
    view_cap = VIEW_ITEM_CAP if fs else TOTAL_ITEM_CAP
    if len(fs) > fact_cap:
        flags.append(f"digest-truncated: 第{ch_no}章 事实 {len(fs)} 条截断至 {fact_cap} (按期间新旧保留)")
        fs = fs[:fact_cap]
    if len(vs) > view_cap:
        flags.append(f"digest-truncated: 第{ch_no}章 观点 {len(vs)} 条截断至 {view_cap}")
        vs = vs[:view_cap]
    ftext = "\n".join(f"- [E:{f.eid}] {f.content}｜来源: {f.source}" +
                      (f"｜期间: {f.period}" if f.period else "") for f in fs)
    vtext = "\n".join(f"- [V:{v.vid}] {v.org}({v.publish_date}): {v.statement}" for v in vs)
    over = len(ftext) + len(vtext) - char_cap
    if over > 0:
        keep_f = len(ftext) - over
        if keep_f >= 0:
            cut = ftext.rfind("\n", 0, keep_f)
            ftext = ftext[: cut if cut > 0 else keep_f]
        else:                                  # the view digest alone exceeds the cap; rare but handled
            cut = vtext.rfind("\n", 0, char_cap)
            vtext = vtext[: cut if cut > 0 else char_cap]
            ftext = ""
        flags.append(f"digest-too-long: 第{ch_no}章 证据摘要超 {char_cap} 字符已截断")
    return ftext, vtext, flags


# ---------- chapter generation ----------

def _generate_chapter_body(ch: Dict[str, Any], facts_digest: str,
                           views_digest: str, llm: Any) -> Tuple[str, str]:
    """One chapter's LLM call (non-streaming): success returns (body, ""); both attempts
    failing returns ("", reason).

    T05's chapter_prompt already embeds STYLE_PROMPT and the hypotheses-chapter JSON
    instruction; if the template someday lacks the hypotheses-JSON request, the fallback
    instruction is appended (normally not triggered).
    """
    prompt = chapter_prompt(ch, facts_digest, views_digest)
    if int(ch["no"]) == HYPOTHESES_CHAPTER_NO and ("假设" not in prompt or "```json" not in prompt):
        prompt += _HYPOTHESES_INSTRUCTION
    last_err = ""
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            resp = (llm.invoke(prompt + ("" if attempt == 1 else _RETRY_NUDGE)) or "").strip()
            if resp:
                m = _FENCE_WRAP_RE.match(resp)          # models habitually fence the whole body; strip it
                return (m.group(1).strip() if m else resp), ""
            last_err = "空响应"
        except Exception as e:                           # network/rate-limit/timeout etc.; record reason, then retry
            last_err = f"{type(e).__name__}: {e}"
        logger.warning("第%s章<%s> 第 %s/%s 次尝试失败: %s",
                       ch["no"], ch["title"], attempt, RETRY_TIMES, last_err)
    return "", last_err


def _iter_json_array_candidates(text: str):
    """Yield bracket-balanced candidate JSON-array strings in '[' order of appearance
    (outer arrays first; nested arrays not mis-cut)."""
    for start, ch in enumerate(text):
        if ch != "[":
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def _parse_hypotheses(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Strip the ```json-fenced hypotheses array from the hypotheses chapter's output
    -> (body, hypotheses dict list).

    Lenient parsing (AGENTS discipline: strip fences): fenced blocks first; if all fail,
    do bare-array bracket balancing (a candidate counts as a hit only when it parses into
    a dict array with non-empty statements). Body = the remaining text minus the matched
    JSON block.
    """
    items: List[Dict[str, Any]] = []
    for block in _JSON_ARRAY_FENCE_RE.findall(text):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            items += [d for d in data if isinstance(d, dict) and str(d.get("statement", "")).strip()]
    body = _JSON_ARRAY_FENCE_RE.sub("", text).strip()
    if not items:
        for cand in _iter_json_array_candidates(body):
            try:
                data = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, list):
                continue
            cand_items = [d for d in data
                          if isinstance(d, dict) and str(d.get("statement", "")).strip()]
            if cand_items:
                items = cand_items
                body = body.replace(cand, "", 1).strip()
                break
    return body, items


def _build_hypotheses(items: List[Dict[str, Any]], start_index: int = 0) -> List[Hypothesis]:
    """dict -> Hypothesis (hid numbered H1.. from start_index+1; indicators/refs washed into non-empty str lists)."""
    out: List[Hypothesis] = []
    for i, d in enumerate(items, start_index + 1):
        out.append(Hypothesis(
            hid=f"H{i}",
            statement=str(d.get("statement", "")).strip(),
            indicators=[str(x).strip() for x in d.get("indicators", []) if str(x).strip()],
            related_eids=[str(x).strip() for x in d.get("related_eids", []) if str(x).strip()],
        ))
    return out


def _dedup_flags(flags: List[str]) -> List[str]:
    seen: set = set()
    return [f for f in flags if not (f in seen or seen.add(f))]


# ---------- main entry ----------

def generate_draft(stock: str, years: int = 5,
                   research_dir: Optional[Path] = None,
                   llm: Any = None) -> Draft:
    """Generate the ten-chapter draft chapter by chapter, persist draft.json, return Draft.

    stock: stock name or ts_code; years: evidence look-back years (out-of-window evidence
    dropped, only logged); research_dir: evidence directory (default
    output/research/<ts_code>); llm: LLM client stub (must have .invoke(prompt)->str),
    defaulting to lazily-loaded get_agent_llm().
    """
    root = Path(PathManager().output_dir)
    ts_code, name, rdir = resolve_stock(stock, root, research_dir)
    if not rdir.is_dir() or not list(rdir.glob("*.json")):
        raise FileNotFoundError(f"证据目录为空: {rdir} — 请先运行 Stage 1 (T02/T03/T04) 生成分片")
    facts, views = load_pack(rdir)
    logger.info("载入证据: %d facts / %d views (%s)", len(facts), len(views), rdir)

    now_year = datetime.now().year
    floor = now_year - years + 1
    facts = [f for f in facts if (_fact_year(f) or floor) >= floor]
    views = [v for v in views if (_view_year(v) or floor) >= floor]
    logger.info("回看 %d 年 (>= %d): 剩余 %d facts / %d views", years, floor, len(facts), len(views))
    if not facts and not views:
        raise ValueError(f"{rdir} 内无 {floor} 年后的证据, 拒绝生成空稿 (可调大 years)")

    if llm is None:
        from harness.llm import get_agent_llm     # lazy import: offline tests don't init the OpenAI client
        llm = get_agent_llm(timeout=300.0)        # long-form + thinking generation; the default 120s is tight

    flags: List[str] = []
    chapters: List[Chapter] = []
    hypotheses: List[Hypothesis] = []
    for ch in CHAPTERS:
        no = int(ch["no"])
        sel_f, sel_v, sel_flags = select_chapter_evidence(ch.get("wants", []), facts, views)
        flags += sel_flags
        facts_digest, views_digest, dflags = build_digest(no, sel_f, sel_v)
        flags += dflags
        body, err = _generate_chapter_body(ch, facts_digest, views_digest, llm)
        if err:
            flags.append(f"chapter-failed: 第{no}章<{ch['title']}> 跳过 ({err})")
            continue
        # hypotheses-JSON parsing is a fallback for every chapter (only the hypotheses
        # chapter's prompt asks for it; a hit elsewhere is a bonus)
        body, hitems = _parse_hypotheses(body)
        if hitems:
            hypotheses += _build_hypotheses(hitems, start_index=len(hypotheses))
        used: List[str] = []
        for _, ref in extract_citations(body):
            if ref not in used:
                used.append(ref)
        chapters.append(Chapter(no=no, title=str(ch["title"]), body_md=body, used_eids=used))
        logger.info("第%s章<%s>完成: %d 字, 引用 %d 处", no, ch["title"], len(body), len(used))
    if not hypotheses:
        flags.append(f"hypotheses-missing: 第{HYPOTHESES_CHAPTER_NO}章(假设章) "
                     f"未解析出假设 JSON 数组")

    draft = Draft(ts_code=ts_code, name=name,
                  generated_at=datetime.now().isoformat(timespec="seconds"),
                  chapters=chapters, hypotheses=hypotheses, flags=[])
    flags += check_draft(draft, facts, views)
    flags += [f"contract-invalid: {e}" for e in validate_draft(draft, facts, views)]
    draft.flags = _dedup_flags(flags)
    save_draft(draft, rdir / "draft.json")
    logger.info("草稿完成: %d/%d 章, %d 假设, %d flags → %s",
                len(chapters), len(CHAPTERS), len(hypotheses), len(draft.flags),
                rdir / "draft.json")
    return draft


# ---------- CLI ----------

def _cli(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m skills.research.compose.generator",
        description="研究生产线 Stage 2: 证据包 → 十章草稿 draft.json (LLM 逐章生成)")
    ap.add_argument("stock", help="股票名 (如 贵州茅台) 或 ts_code (如 600519.SH)")
    ap.add_argument("--years", type=int, default=5, help="证据回看年数 (默认 5)")
    ap.add_argument("--dir", type=Path, default=None,
                    help="证据目录 (默认 output/research/<ts_code>)")
    args = ap.parse_args(argv)
    draft = generate_draft(args.stock, years=args.years, research_dir=args.dir)
    print(f"草稿已生成: {draft.ts_code} {draft.name} — "
          f"{len(draft.chapters)} 章, {len(draft.hypotheses)} 条假设, {len(draft.flags)} 条 flags")
    for f in draft.flags[:20]:
        print(" -", f)
    if len(draft.flags) > 20:
        print(f"  …其余 {len(draft.flags) - 20} 条见 draft.json")


if __name__ == "__main__":
    _cli()
