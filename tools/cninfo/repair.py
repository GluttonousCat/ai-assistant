# -*- encoding: utf-8 -*-
"""
年报结构修复 Agent — 契约违约的"Agent 识别 + Agent 解决"兜底 (漏斗 L2)

分工原则 (用户裁定): 结构问题不追特例正则 — 识别与解决都是 Agent 语义判断;
代码层只剩两类东西:
- 机械装配: 行切片 → {title, text, tables} 同构产物 (schema 保证, 零公司特例)
- 语义判据: contracts 契约机检 (验收), 不过则失败原因回灌重试 (默认 3 次)

链路: 定位(LLM: 真实正文起止行) → 切分(LLM: 小节边界+标题) → 装配(代码) →
契约机检(代码) → 未过回灌重试; 留痕 _items.json "repairs" 键。
目录陷阱/异形编号(A+H 管治守则风格)/无编号小节等结构变体全部由 Agent 语义消化。

LLM 由调用方注入 (与 evidence 内核同约定, 模块本体零项目依赖);
CLI 是注入口:
    python -m tools.cninfo.repair --stock 600036 [--attempts 3] [--dry-run]
    python -m tools.cninfo.repair --all [--limit 10]     # 扫 MD_ROOT 全部违约文件
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .contracts import (CONTRACTS, acceptance_check, check_data, diagnostic_terms,
                        reason_terms, repair_brief)
from .parse import (ITEM_ALIASES, MD_ROOT, SECTION_RULES, _clean_title,
                    _header_lines, _subsection_dict, marker_rank)

LOCATE_PROMPT = """你是年报结构修复定位器。在带行号的年报大纲中, 定位目标内容的真实正文位置。

{brief}

定位规则:
1. 严格只输出一个 JSON (无解释无代码块):
   {{"found": true, "title": "命中的标题原文", "start": 行号, "end": 行号, "confidence": 0.0}}
2. start = 目标块首行 (含标题行本身); end = 目标块末行 (下一个同级标题之前, 含表格末行)
3. 目录行 (前缀形如 "- 13 第二章…" 或行内只有章名+页码) 不是正文 — 真正文在目录之后
4. {kind_hint}
5. 实在找不到给 {{"found": false}}
{feedback}

大纲 (L行号: 内容, 已截断):
{outline}"""

_KIND_HINT = {
    "item": "目标是第二章内的一个表格块: 别名标题行到下一小节标题前, 必须含数据表格本体。",
    "section": "目标是一个完整章节: 章节标题行到下一章节标题行之前, 含全部小节。",
}

SEGMENT_PROMPT = """你是年报小节结构分析器。对下面带相对行号的章节正文做语义切分。

规则:
1. 严格只输出一个 JSON 数组 (无解释无代码块):
   [{{"title": "小节标题", "start": 行号, "end": 行号}}, ...]
2. 小节标题形态不限: 中文编号(一、)/数字编号(1 / 2.3)/字母编号(B / B.3 / E.)/
   加粗行/#### 标记行 — 语义判断"该行是不是小节标题", 不依赖单一形态
3. 各小节首尾相接覆盖整个区间, 标题行是该小节首行
4. 章节开头 (第一个小节标题之前) 若有实质内容 (如股东总数表), 输出
   {{"title": "(节首前文)", "start": 1, "end": 首标题前一行的行号}} 作为第一项
5. 标题去编号前缀保留语义 ('B.3 提名委员会'→'提名委员会', '1 本报告期…'→'本报告期…')
6. 整章纯段落无小节结构给 []
{feedback}

章节正文 (R行号: 内容, 每行已截断):
{block}"""


def _outline(md: str, key: str, cap: int = 160) -> str:
    """带行号大纲: 标题特征行 + 含目标词/探针的行 (给 LLM 的全部视野)"""
    c = CONTRACTS[key]
    terms = list(ITEM_ALIASES.get(key) or [])
    terms.append(c["title"])
    terms += (SECTION_RULES.get(key) or {}).get("keywords") or []
    out: List[str] = []
    for i, line in enumerate(md.split("\n"), 1):
        s = line.strip()
        if not s:
            continue
        head_like = (marker_rank(s) is not None or s.startswith("#")
                     or re.match(r"^[-–—]?\s*\d+\s*(第|[一二三四五六七八九十]|附录)", s))
        if head_like or (len(s) <= 46 and any(t in s for t in terms)):
            out.append(f"L{i}: {s[:72]}")
            if len(out) >= cap:
                break
    return "\n".join(out)


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.startswith("json"):
            t = t[4:]
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _locate(llm, kind: str, key: str, md: str, feedback: str) -> Optional[Dict]:
    prompt = LOCATE_PROMPT.format(
        brief=repair_brief(kind, key), kind_hint=_KIND_HINT[kind],
        feedback=feedback, outline=_outline(md, key))
    try:
        resp = llm.invoke([{"role": "user", "content": prompt}])
    except Exception as e:  # noqa: BLE001 LLM 故障按"本轮未找到"处理, 不中断批次
        return {"found": False, "error": str(e)[:80]}
    loc = _parse_json(resp or "")
    if loc and loc.get("found"):
        for k in ("start", "end"):
            try:
                loc[k] = int(loc[k])
            except (TypeError, ValueError):
                return {"found": False, "error": f"{k} 非法"}
    return loc


def _segment(llm, block_lines: List[str], feedback: str) -> Optional[List[Dict]]:
    """Agent 切分: 章节正文 → [{title, start, end}] (相对行号); 失败返回 None"""
    numbered = "\n".join(f"R{i}: {l[:100]}" for i, l in enumerate(block_lines, 1))
    try:
        resp = llm.invoke([{"role": "user", "content": SEGMENT_PROMPT.format(
            feedback=feedback, block=numbered)}])
    except Exception as e:  # noqa: BLE001
        return None
    t = (resp or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.startswith("json"):
            t = t[4:]
    try:
        segs = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", t, re.DOTALL)
        if not m:
            return None
        try:
            segs = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(segs, list):
        return None
    n = len(block_lines)
    out = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        try:
            a, b = int(s["start"]), int(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        a, b = max(1, a), min(n, max(a, b))
        out.append({"title": str(s.get("title") or "").strip()[:80] or f"R{a}",
                    "start": a, "end": b})
    return sorted(out, key=lambda s: s["start"]) or None


def _assemble(kind: str, key: str, block_lines: List[str], segs: Optional[List[Dict]],
              title: str, md: str) -> Dict:
    """机械装配: 行切片 → 与主链路同构的 {title, text, tables} (零公司特例)"""
    headers = _header_lines(md)
    block = "\n".join(block_lines)
    if kind == "item" or not segs:
        t = _clean_title((title or "").strip() or block.split("\n", 1)[0])
        return (_subsection_dict(t, block, headers) if kind == "item"
                else {"title": _clean_title(t) or CONTRACTS[key]["title"],
                      "subsections": [_subsection_dict(
                          t or "全文", block, headers)]})
    subs = [_subsection_dict(_clean_title(s["title"]),
                            "\n".join(block_lines[s["start"] - 1:s["end"]]),
                            headers) for s in segs]
    return {"title": _clean_title((title or "").strip() or CONTRACTS[key]["title"]),
            "subsections": subs}


def repair_key(llm, kind: str, key: str, md: str, attempts: int = 3
               ) -> Tuple[bool, Optional[Dict], List[str]]:
    """单键修复: Agent 定位 → Agent 切分 → 机械装配 → 契约机检, 失败原因回灌重试"""
    lines = md.split("\n")
    feedback = ""
    seg_feedback = ""
    log: List[str] = []
    for attempt in range(1, attempts + 1):
        loc = _locate(llm, kind, key, md, feedback)
        if not loc or not loc.get("found"):
            log.append(f"a{attempt}: 未定位 ({(loc or {}).get('error', 'found=false')})")
            feedback = ("上一轮未找到目标。放宽匹配: 参考词表与异形标题提示, "
                        "注意'财务概要/摘要'类变体与无编号小节结构。")
            continue
        start = max(1, min(int(loc["start"]), len(lines)))
        end = max(start, min(int(loc["end"]), len(lines)))
        block_lines = lines[start - 1:end]
        segs: Optional[List[Dict]] = None
        if kind == "section":
            segs = _segment(llm, block_lines, seg_feedback)
            if segs is None:
                log.append(f"a{attempt}: L{start}-{end} 切分失败 (JSON 解析不过)")
                seg_feedback = "上一轮切分输出无法解析; 严格输出 JSON 数组, 行号用整数。"
                continue
        entry = _assemble(kind, key, block_lines, segs,
                          str(loc.get("title") or ""), md)
        verdict = acceptance_check(kind, key, entry)
        if verdict["ok"]:
            n = len(entry.get("subsections") or [])
            log.append(f"a{attempt}: L{start}-{end} 过验收 ({verdict['reason']}, {n} 小节)")
            return True, entry, log
        log.append(f"a{attempt}: L{start}-{end} 未过: {verdict['reason']}")
        feedback = (f"上一轮定位未过验收: {verdict['reason']}。"
                    "检查: 是否锚到目录行(前缀'- N 第X章')、end 是否切在表格中间、"
                    "start 是否落在真实标题而非页眉。")
        seg_feedback = (f"上一轮切分未满足契约: {verdict['reason']}。"
                        "检查: 小节标题是否保留了语义关键词 (如 股权激励/审计委员会/担保), "
                        "节首前文是否单独成项。")
    return False, None, log


def repair_file(path: Path, llm, attempts: int = 3,
                dry_run: bool = False) -> Dict:
    """单文件: 契约检查 → 逐违约键修复 → 回写 (含 repairs 审计) → 复检"""
    d = json.loads(path.read_text(encoding="utf-8"))
    before = check_data(d)
    md_path = path.with_name(path.name.replace("_items.json", ".md"))
    if not md_path.exists():
        return {"file": str(path), "error": f"缺 MD: {md_path.name}"}
    md = md_path.read_text(encoding="utf-8")

    repairs = d.get("repairs") or {}
    for key, reason in before["fails"].items():
        c = CONTRACTS[key]
        bucket = "items" if c["kind"] == "item" else "sections"
        ok, entry, log = repair_key(llm, c["kind"], key, md, attempts=attempts)
        if ok and not dry_run:
            d[bucket][key] = entry
        repairs[key] = {"attempts": len(log), "ok": ok, "log": log}
    if not dry_run:
        d["repairs"] = repairs
        path.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    after = check_data(d)
    return {"file": str(path), "fails_before": before["fails"],
            "fails_after": after["fails"], "repairs": repairs}


def repair_stock(stock: str, llm, attempts: int = 3,
                 dry_run: bool = False) -> Dict:
    from .parse import normalize_ts_code
    ts_code = normalize_ts_code(stock.strip())
    files = sorted(MD_ROOT.glob(f"{ts_code}/*_items.json"))
    if not files:
        return {"error": f"{stock} 无 _items.json (先跑 tools.cninfo.parse)"}
    return repair_file(files[0], llm, attempts=attempts, dry_run=dry_run)


def repair_all(llm, attempts: int = 3, limit: int = 0,
               dry_run: bool = False) -> List[Dict]:
    """扫全部产物: 只修有契约违约的文件"""
    results = []
    for p in sorted(MD_ROOT.glob("*/*_items.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if check_data(d)["fails"]:
            results.append(repair_file(p, llm, attempts=attempts, dry_run=dry_run))
            if limit and len(results) >= limit:
                break
    return results


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    ap = argparse.ArgumentParser(description="年报契约违约修复 Agent")
    ap.add_argument("--stock", help="单股 ts_code/6位代码")
    ap.add_argument("--all", action="store_true", help="扫全部违约文件")
    ap.add_argument("--attempts", type=int, default=3, help="重试上限 (默认3)")
    ap.add_argument("--limit", type=int, default=0, help="--all 时最多处理文件数")
    ap.add_argument("--dry-run", action="store_true", help="只试不写")
    args = ap.parse_args()

    from core.config import get_config
    get_config()
    from harness.llm import get_extract_llm
    _llm = get_extract_llm(timeout=120.0)

    if args.stock:
        r = repair_stock(args.stock, _llm, attempts=args.attempts,
                         dry_run=args.dry_run)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif args.all:
        rs = repair_all(_llm, attempts=args.attempts, limit=args.limit,
                        dry_run=args.dry_run)
        n_ok = sum(1 for r in rs if not r.get("fails_after"))
        print(f"\n修复批次: {len(rs)} 文件, 复检全过 {n_ok}")
        for r in rs:
            mark = "✔" if not r.get("fails_after") else "✘"
            rel = "/".join(Path(r["file"]).parts[-2:])
            print(f"{mark} {rel}: {list(r.get('fails_before', {}))} → "
                  f"{list(r.get('fails_after', {}))}")
    else:
        print(__doc__)
