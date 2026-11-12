# -*- encoding: utf-8 -*-
"""
Section content contracts — the single declaration of, per item/section,
"why it is extracted, what it must contain, what counts as passing".

One declaration, three consumers:
- repair-Agent task brief   repair_brief(key) → prompt fragment (fallback for
                            structurally non-conforming annual reports)
- validator pass criteria   acceptance_check() → deterministic machine check, zero LLM
- manual-review checklist   humans read intent / must / accept directly

Layering versus parse.py: SECTION_RULES / ITEM_ALIASES are "how to slice"
(operational rules: vocab/patterns/guards); this module is "what/why/pass-or-
not" (semantic contracts). Slicing upgrades never touch contracts; contract
changes never touch slicing. Acceptance-side probe words reference the parse
vocab (single source of truth); contract-owned fields only add semantics.

CLI:
    python -m tools.cninfo.contracts --list            # print contracts for humans
    python -m tools.cninfo.contracts --check [path]    # run compliance checks on _items.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .parse import ITEM_ALIASES, MD_ROOT

# ---------------- contract declarations ----------------
# Keys with optional=True may be missing entirely (e.g. bonds "not applicable");
# any other missing key is a violation.

CONTRACTS: Dict[str, Dict] = {
    # ---- the two exact items of chapter 2 (items) ----
    "kpi3y": {
        "kind": "item", "title": "主要会计数据和财务指标",
        "intent": "近三年主要会计数据与财务指标 (营收/归母净利/净资产/ROE/EPS 等逐年行), "
                  "估值与财务分析的基础数据源; 分季度口径不走本项 (走 Tushare SQL)。",
        "must": ["含三年年份维度的指标行或一张'主要会计数据'表 (≥3 数据行)"],
        "accept": "text 中出现 ≥3 个不同年份, 或 tables 任一表 ≥3 行",
        "repair_hints": ["异形标题: 财务概要/财务摘要/主要财务数据及指标/会计数据和财务指标摘要",
                          "年报缺失时半年报第二章同构可顶上"],
    },
    "nonrecurring": {
        "kind": "item", "title": "非经常性损益项目和金额",
        "intent": "非经常性损益分项明细 (政府补助/处置收益等) + 合计 + 所得税/少数股东影响, "
                  "扣非净利润口径的依据。",
        "must": ["分项明细 (≥3 个项目行)", "合计行"],
        "accept": "text 含'合计'且任一 table ≥3 行",
        "repair_hints": ["短块合法 (实证: 兴业 '2.3 非经常性损益' 仅 243 字)",
                          "目录条目是噪声: 短且无表格的命中应弃"],
    },
    # ---- chapters 3-7 (sections) ----
    "mdna": {
        "kind": "section", "title": "管理层讨论与分析",
        "intent": "管理层对经营情况的讨论与分析: 行业格局/主营业务/经营回顾/风险因素。"
                  "用户拍板'全部重要' — 按 L1 小节全量抽取, 不做关键词筛选。",
        "must": ["经营情况讨论 (回顾/分析类小节)", "风险因素小节 (强烈期望, 个别公司异位可容忍)"],
        "accept": "subsections ≥1, 且存在标题含 经营/回顾/分析/讨论 的小节",
        "repair_hints": ["同键多节合并合法 (实证: 中石化=业绩回顾/管理层讨论/董事会报告 三主题节拼接)",
                          "个别公司风险因素写在'重要事项'节, 此时 matters 里出现风险小节不算错"],
    },
    "governance": {
        "kind": "section", "title": "公司治理",
        "intent": "董监高任职/薪酬/变动、员工结构、股权激励、专门委员会运作。",
        "must": ["董监高情况 (任职/薪酬/变动任一)"],
        "accept": "存在探针词命中的小节 (探针词取自 parse.SECTION_RULES 同源词表)",
        "repair_hints": ["部分公司治理节无编号小节, 按语义段切"],
    },
    "matters": {
        "kind": "section", "title": "重要事项",
        "intent": "募集资金使用/承诺履行/关联交易/担保/诉讼/退市风险/非标意见。",
        "must": ["上述任一主题的实际内容"],
        "accept": "存在探针词命中的小节",
        "repair_hints": ["主题式年报可能把'承诺与披露'并入其他节, 按语义找"],
    },
    "shareholders": {
        "kind": "section", "title": "股份变动及股东情况",
        "intent": "股东大会召开与表决 + 股东总数与前述股东关联关系; "
                  "节首前文 = 股东总数字段 (普通股股东总数表), 必须保留。",
        "must": ["股东总数数值 或 股东大会表决结果"],
        "accept": "subsections ≥1, 且节首前文含'股东总数'或任一小节含表格/数值",
        "repair_hints": ["节首前文 (include_head) 是本节特有语义: 无编号小节开头即为股东总数表"],
    },
    "bonds": {
        "kind": "section", "title": "债券相关情况", "optional": True,
        "intent": "发行债券情况 (规模/利率/偿还); 多数非发债公司整节为'不适用'。",
        "must": [],
        "accept": "可空; <500 字按'不适用'合法跳过, 有内容则全量",
        "repair_hints": [],
    },
}

# ---------------- machine-check criteria (deterministic, zero LLM) ----------------

_YEAR = re.compile(r"(?:19|20)\d{2}")


def _n_rows(table: Dict) -> int:
    rows = table.get("rows")
    return len(rows) if isinstance(rows, list) else 0


def _max_rows(extracted: Dict) -> int:
    return max((_n_rows(t) for t in extracted.get("tables") or []), default=0)


def _all_text(extracted: Dict) -> str:
    """text + table headers and all cells — keywords/years often live only in
    tables (text has tables stripped by _clean_text; observed: Ping An Bank
    nonrecurring '合计' appears inside a row)"""
    parts = [extracted.get("text") or ""]
    for t in extracted.get("tables") or []:
        parts.append(" ".join(t.get("header") or []))
        for row in t.get("rows") or []:
            parts.append(" ".join(str(c) for c in row))
    return " ".join(parts)


def _subs(section: Optional[Dict]) -> List[Dict]:
    return (section or {}).get("subsections") or []


def _check_item(key: str, d: Optional[Dict]) -> tuple:
    if d is None:
        return False, "整项缺失"
    full = _all_text(d)
    if key == "kpi3y":
        if len(set(_YEAR.findall(full))) >= 3 or _max_rows(d) >= 3:
            return True, "年份/表格维度达标"
        return False, "未见到三年年份或多行表格"
    if key == "nonrecurring":
        if "合计" in full and _max_rows(d) >= 3:
            return True, "含合计且有明细行"
        return False, "缺'合计'行或明细不足"
    return True, ""


def _probe_words(key: str) -> List[str]:
    """Acceptance probe words: same source as the parse.SECTION_RULES slicing
    vocab (single source of truth)."""
    from .parse import SECTION_RULES
    return (SECTION_RULES.get(key) or {}).get("keywords") or []


def _check_section(key: str, d: Optional[Dict]) -> tuple:
    if d is None:
        if CONTRACTS[key].get("optional"):
            return True, "可空节, 缺失合法"
        return False, "整节缺失"
    subs = _subs(d)
    if key == "mdna":
        if subs and any(w in (s.get("title") or "") for s in subs
                        for w in ("经营", "回顾", "分析", "讨论")):
            return True, f"{len(subs)} 小节, 含经营讨论"
        if subs:
            return False, f"{len(subs)} 小节但无经营/讨论类标题 (疑切错节)"
        return False, "无小节"
    if key in ("governance", "matters"):
        probes = _probe_words(key)
        hit = any(w in (s.get("title") or "") for s in subs for w in probes)
        return (True, f"{len(subs)} 小节, 探针命中") if hit \
            else (False, f"{len(subs)} 小节但探针词零命中")
    if key == "shareholders":
        head = next((s for s in subs if s.get("title") == "(节首前文)"), None)
        if head and "股东总数" in (head.get("text") or ""):
            return True, "节首前文含股东总数"
        if any(s.get("tables") for s in subs):
            return True, "含表决/持股表"
        return False, "未见股东总数字段或表格"
    if key == "bonds":
        body = "".join((s.get("text") or "") for s in subs)
        if len(body.strip()) < 500:
            return True, "短节按'不适用'容忍"
        return True, f"{len(subs)} 小节"
    return True, ""


def acceptance_check(kind: str, key: str, extracted: Optional[Dict]) -> Dict:
    """Contract machine-check entry. kind: 'item'|'section'; extracted is the
    product for that key in _items.json (or None=missing). Returns
    {ok, reason} — shared by the repair Agent and batch compliance sweeps."""
    if key not in CONTRACTS:
        return {"ok": False, "reason": f"未知契约键: {key}"}
    ok, reason = (_check_item if kind == "item" else _check_section)(key, extracted)
    return {"ok": ok, "reason": reason}


# ---------------- repair-Agent task brief ----------------

_REASON_TERM = re.compile(r"[\u4e00-\u9fff]{2,6}")


def diagnostic_terms(key: str) -> List[str]:
    """Diagnostic keywords from the contract must/accept text (e.g.
    nonrecurring's '合计') — injected into the repair Agent's outline scan so
    that "what must exist" enters its field of view directly (cross-page
    orphan lines/fragmented tables are invisible at the title level; they are
    caught via content words)."""
    c = CONTRACTS[key]
    seen: List[str] = []
    for text in (c.get("must") or []) + [c.get("accept") or ""]:
        for t in _REASON_TERM.findall(text):
            if t not in seen and t not in ("达标", "合法", "存在", "缺失", "整项", "整节"):
                seen.append(t)
    return seen[:8]


def reason_terms(reason: str) -> List[str]:
    """Keywords from the acceptance-failure reason — attention redirection for
    retry rounds (Ping An case: '缺合计行' → 合计)."""
    return [t for t in _REASON_TERM.findall(reason or "")
            if t not in ("达标", "合法", "存在", "缺失", "整项", "整节", "未过", "或")][:6]

def repair_brief(kind: str, key: str) -> str:
    """Contract → Agent prompt fragment: tell the repair Agent what to look
    for and what counts as passing."""
    c = CONTRACTS[key]
    aliases = ITEM_ALIASES.get(key) or _probe_words(key) or []
    lines = [f"目标: {c['title']} ({kind})", f"意图: {c['intent']}"]
    if c["must"]:
        lines.append("必须含: " + "; ".join(c["must"]))
    lines.append(f"合格判据: {c['accept']}")
    if aliases:
        lines.append("已知标题词/探针 (异形时可参考): " + "/".join(aliases[:8]))
    if c["repair_hints"]:
        lines.extend(f"提示: {h}" for h in c["repair_hints"])
    return "\n".join(lines)


# ---------------- batch compliance sweep ----------------

def check_data(d: Dict) -> Dict:
    """In-memory items product → {fails: {key: reason}, n_keys, n_ok} (for
    repair-loop re-checks)."""
    fails: Dict[str, str] = {}
    n_ok = 0
    for key, c in CONTRACTS.items():
        bucket = "items" if c["kind"] == "item" else "sections"
        r = acceptance_check(c["kind"], key, (d.get(bucket) or {}).get(key))
        if r["ok"]:
            n_ok += 1
        else:
            fails[key] = r["reason"]
    return {"fails": fails, "n_keys": len(CONTRACTS), "n_ok": n_ok}


def check_file(path: Path) -> Dict:
    """A single _items.json → {file, fails: {key: reason}, n_keys, n_ok}."""
    d = json.loads(path.read_text(encoding="utf-8"))
    r = check_data(d)
    return {"file": str(path), **r}


def check_all(root: Path = MD_ROOT) -> List[Dict]:
    return [check_file(p) for p in sorted(root.glob("*/*_items.json"))]


def _cli() -> None:
    args = sys.argv[1:]
    if "--list" in args:
        for key, c in CONTRACTS.items():
            print(f"\n[{key}] {c['title']}  ({c['kind']}"
                  f"{' / 可空' if c.get('optional') else ''})")
            print(f"  意图: {c['intent']}")
            for m in c["must"]:
                print(f"  必须: {m}")
            print(f"  判据: {c['accept']}")
            for h in c["repair_hints"]:
                print(f"  提示: {h}")
        return
    if "--check" in args:
        i = args.index("--check")
        target = Path(args[i + 1]) if len(args) > i + 1 else MD_ROOT
        files = ([target] if target.is_file()
                 else sorted(target.glob("*/*_items.json")))
        n_fail_files = 0
        agg: Dict[str, int] = {}
        for p in files:
            r = check_file(p)
            if r["fails"]:
                n_fail_files += 1
                for k in r["fails"]:
                    agg[k] = agg.get(k, 0) + 1
                rel = "/".join(Path(r["file"]).parts[-2:])
                print(f"FAIL {rel}: " + "; ".join(
                    f"{k}({v})" for k, v in r["fails"].items()))
        print(f"\n契约合规: {len(files) - n_fail_files}/{len(files)} 文件全过"
              f"; 违约分布: {json.dumps(agg, ensure_ascii=False)}")
        return
    print(__doc__)


if __name__ == "__main__":
    _cli()
