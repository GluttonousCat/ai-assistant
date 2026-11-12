# -*- encoding: utf-8 -*-
"""
T03 · fine-grained annual-report evidence extraction (Stage 1, fact layer)

Input:  output/cninfo/md/<ts_code>/<year>_ndbg_items.json
        (produced by tools/cninfo/parse.py; this module only reads it; each section in
        `sections` carries title/text/tables)
Output: EvidenceShard(kind="annual") -> output/research/<ts_code>/facts_annual_<year>.json
        + manual spot-check review list annual_review_<year>.md (random 10% sample of
        extracted sentence <-> original sentence pairs)

Layering (since 2026-10): the extraction domain kernel — system prompt / subsection
serialization / atom sanitization — lives in the evidence-extraction section of
tools/cninfo/parse.py (self-contained; the LLM client is injected by this module as
get_extract_llm); this module only does research-side contract orchestration: eid
generation (A:<year>.<section>.<subsection no>.<seq>, never trusting LLM-generated IDs) /
EvidenceShard assembly / contract-validation fallback / the review list / writing to the
research directory.

Discipline (per the task spec and T00 iron rules): extract only what the original text
explicitly states, inference forbidden, numbers copied verbatim, every entry carries a
subsection anchor for traceability (source=《年报》·section·subsection title), verbatim
quotes get confidence=1.0, converted values 0.9 with the formula noted.

CLI: python -m skills.research.sources.annual_source 600519.SH 2025
"""
from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.paths import PathManager
from harness.llm import get_extract_llm
from skills.research.contracts import (
    EvidenceShard,
    Fact,
    research_dir,
    save_shard,
    validate_shard,
)
from tools.cninfo.parse import (CATEGORIES, CATEGORY_CN, MIN_BODY_CHARS,
                                   SECTION_ORDER, build_subsection_body,
                                   extract_subsection, sanitize_fact)

logger = get_logger(__name__)


# ---------- input ----------

def annual_items_path(ts_code: str, year: int) -> Path:
    """output/cninfo/md/<ts_code>/<年>_ndbg_items.json"""
    return Path(PathManager().output_dir) / "cninfo" / "md" / ts_code / f"{year}_ndbg_items.json"


def load_annual_items(items_path: Path) -> Dict[str, Any]:
    if not items_path.exists():
        raise FileNotFoundError(
            f"年报 items 不存在: {items_path} (先运行 python -m tools.cninfo.parse 生成)")
    data = json.loads(items_path.read_text(encoding="utf-8"))
    if not isinstance(data.get("sections"), dict):
        raise ValueError(f"items 缺少 sections 结构: {items_path}")
    return data


# ---------- contract enforcement ----------

def _enforce_contract(shard: EvidenceShard, dropped: List[Dict[str, Any]]) -> None:
    """Contract fallback: drop one by one every fact that validate_shard reports (residue
    still violating the rules after sanitize)"""
    for _ in range(200):                          # defensive upper bound
        errors = validate_shard(shard)
        if not errors:
            return
        m = re.search(r"facts\[(\d+)\]", errors[0])
        if not m:
            return
        idx = int(m.group(1))
        dropped.append({"eid": shard.facts[idx].eid,
                        "reason": errors[0].split(" ", 1)[-1][:120]})
        shard.facts.pop(idx)
    logger.warning("契约兜底剔除达上限仍有错误: %s", validate_shard(shard)[:3])


# ---------- review list ----------

def write_review_md(path: Path, shard: EvidenceShard, year: int, seed: str,
                    sample_size: int) -> List[str]:
    """Random-10% spot-check list: extracted sentence <-> original sentence pairs (the
    auditable form of the quality commitment). Original sentences live in
    shard.meta["quotes"] (the Fact contract has no quote field, so not on Fact)."""
    facts = shard.facts
    rng = random.Random(seed)
    sample = rng.sample(facts, min(sample_size, len(facts)))
    cat_of = shard.meta.get("fact_categories", {})
    quotes = shard.meta.get("quotes", {})
    lines = [
        f"# 年报证据抽检复核清单 — {shard.ts_code} {year}年报",
        "",
        f"> 生成时间: {datetime.now():%Y-%m-%d %H:%M} · "
        f"抽样 {len(sample)}/{len(facts)} 条 (随机 10%, seed={seed})",
        "> 用法: 逐条对照「抽取句」与「原句」; 数字不一致、有推断改写或单位口径丢失的, "
        "勾掉复选框并在行尾批注。全部通过即可签收本分片。",
        "",
    ]
    for f in sample:
        lines.append(f"## {f.eid} · {CATEGORY_CN.get(cat_of.get(f.eid, ''), cat_of.get(f.eid, ''))}")
        lines.append(f"- 抽取句: {f.content}")
        quote = quotes.get(f.eid, "")
        lines.append(f"- 原句: {quote or '⚠️ (LLM 未提供原句, 需人工按 source 定位核对)'}")
        lines.append(f"- 来源: {f.source} · period={f.period} · confidence={f.confidence}")
        lines.append(f"- [ ] 一致")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return [f.eid for f in sample]


# ---------- main flow ----------

def collect_annual_facts(ts_code: str, year: int, out_dir: Path | None = None,
                         items_path: Path | None = None) -> EvidenceShard:
    """Annual-report items -> atom-level evidence shard (facts_annual_<year>.json + annual_review_<year>.md)"""
    items_path = items_path or annual_items_path(ts_code, year)
    data = load_annual_items(items_path)
    sections: Dict[str, Any] = data["sections"]

    shard = EvidenceShard(kind="annual", ts_code=ts_code)
    cat_of: Dict[str, str] = {}
    quotes: Dict[str, str] = {}
    dropped: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    section_stats: Dict[str, Dict[str, int]] = {}
    llm = None                                       # lazy creation: skip the LLM entirely when all subsections are empty

    for sec_key in SECTION_ORDER:
        subs = (sections.get(sec_key) or {}).get("subsections") or []
        stat = {"subsections": len(subs), "facts": 0, "empty": 0, "llm_failed": 0}
        for sub_idx, sub in enumerate(subs, 1):
            title = str(sub.get("title") or f"小节{sub_idx}").strip()
            body = build_subsection_body(sub)
            if len(body) < MIN_BODY_CHARS:
                stat["empty"] += 1
                skipped.append({"section": sec_key, "title": title, "reason": "empty"})
                continue
            try:
                if llm is None:
                    # 300s: with thinking enabled, a large operations subsection (body + 20-plus tables)
                    # exceeds the default 120s timeout in a single call
                    llm = get_extract_llm(timeout=300.0)
                raws = extract_subsection(llm, year, sec_key, title, body)
            except Exception as e:                  # one subsection failing must not abort the whole run
                stat["llm_failed"] += 1
                skipped.append({"section": sec_key, "title": title,
                                "reason": f"llm_error: {type(e).__name__}: {e}"})
                logger.warning("小节抽取失败跳过 %s·%s: %s", sec_key, title, e)
                continue

            seen_content = set()
            sub_seq = 0                                # eid tail: in-subsection sequence (recounted per subsection)
            for raw in raws:
                clean, reason = sanitize_fact(raw, year)
                if clean is None:
                    dropped.append({"reason": reason, "sample": str(raw.get("content"))[:50]})
                    continue
                if clean["content"] in seen_content:     # dedupe within the same subsection
                    continue
                seen_content.add(clean["content"])
                sub_seq += 1
                eid = f"A:{year}.{sec_key}.{sub_idx}.{sub_seq}"
                source = f"{year}年报·{sec_key}·{title}"
                if clean["confidence"] < 1.0:
                    source += f" [换算: {clean['formula'] or '见原文'}]"
                shard.facts.append(Fact(
                    eid=eid, content=clean["content"], source=source,
                    ts_code=ts_code, period=clean["period"], value=clean["value"],
                    unit=clean["unit"], confidence=clean["confidence"],
                ))
                cat_of[eid] = clean["category"]
                quotes[eid] = clean["quote"]
                stat["facts"] += 1
        section_stats[sec_key] = stat
        logger.info("年报抽取 %s·%s: %d 小节, %d 原子 (%d 空 / %d 失败)",
                    ts_code, sec_key, len(subs), stat["facts"], stat["empty"], stat["llm_failed"])

    _enforce_contract(shard, dropped)
    alive = {f.eid for f in shard.facts}             # after drops, converge the meta side too
    cat_of = {k: v for k, v in cat_of.items() if k in alive}
    quotes = {k: v for k, v in quotes.items() if k in alive}

    cat_counts = {c: sum(1 for v in cat_of.values() if v == c) for c in CATEGORIES}
    out_dir = out_dir or research_dir(Path(PathManager().output_dir), ts_code)
    seed = f"{ts_code}:{year}"
    sample_size = max(1, round(len(shard.facts) * 0.1)) if shard.facts else 0

    shard.meta = {
        "year": year,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_file": str(items_path),
        "sections": section_stats,
        "category_counts": cat_counts,
        "fact_categories": cat_of,
        "quotes": quotes,
        "skipped": skipped,
        "dropped": dropped,
        "review": {"seed": seed, "sample_size": sample_size},
    }
    save_shard(shard, out_dir / f"facts_annual_{year}.json")
    if shard.facts:
        write_review_md(out_dir / f"annual_review_{year}.md", shard, year, seed, sample_size)
    return shard


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        description="年报细粒度证据抽取 (T03): items json → EvidenceShard + 抽检复核清单")
    ap.add_argument("ts_code", help="股票代码, 如 600519.SH")
    ap.add_argument("year", type=int, help="年报年份, 如 2025")
    ap.add_argument("--out-dir", default=None, help="分片输出目录 (默认 output/research/<ts_code>)")
    args = ap.parse_args(argv)

    shard = collect_annual_facts(
        args.ts_code, args.year,
        out_dir=Path(args.out_dir) if args.out_dir else None)

    cc = shard.meta["category_counts"]
    src = shard.meta.get("source_file", "")
    n = len(shard.facts)
    print(f"{args.ts_code} {args.year}年报证据分片完成 (输入: {src})")
    print(f"  facts: {n} 条 "
          f"(经营数据 {cc['operating']} / 分部明细 {cc['segment']} / "
          f"管理层论断 {cc['statement']} / 风险条目 {cc['risk']})")
    sk = shard.meta["skipped"]
    if sk:
        n_empty = sum(1 for s in sk if s["reason"] == "empty")
        print(f"  跳过小节: {len(sk)} (空 {n_empty} / LLM失败 {len(sk) - n_empty})")
    if shard.meta["dropped"]:
        print(f"  契约剔除: {len(shard.meta['dropped'])} 条 (详见分片 meta.dropped)")
    rdir = Path(args.out_dir) if args.out_dir else research_dir(
        Path(PathManager().output_dir), args.ts_code)
    print(f"  分片: {rdir / f'facts_annual_{args.year}.json'}")
    if n:
        print(f"  抽检: {rdir / f'annual_review_{args.year}.md'} "
              f"({shard.meta['review']['sample_size']}/{n} 条)")


if __name__ == "__main__":
    main()
