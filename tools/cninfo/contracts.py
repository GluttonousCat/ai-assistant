# -*- encoding: utf-8 -*-
"""
章节内容契约 — 每项/每节"为什么抽、必须含什么、什么算合格"的单一声明源。

一份声明, 三处消费:
- 修复 Agent 任务说明   repair_brief(key) → prompt 片段 (结构不合规年报的兜底)
- 校验器合格判据       acceptance_check() → 确定式机检, 零 LLM
- 人工复核 checklist   直接人读 intent / must / accept

与 parse.py 的分层: SECTION_RULES / ITEM_ALIASES 是"怎么切" (操作规则: 词表/模式/
守卫), 本模块是"抽什么/为什么/什么算对" (语义契约)。切法升级不碰契约, 契约变更
不碰切法; 验收侧探针词引用 parse 词表 (单一事实源), 契约自有字段只补语义。

CLI:
    python -m tools.cninfo.contracts --list            # 人读契约全文
    python -m tools.cninfo.contracts --check [路径]     # 对 _items.json 跑合规机检
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .parse import ITEM_ALIASES, MD_ROOT

# ---------------- 契约声明 ----------------
# optional=True 的键允许整段缺失 (如债券"不适用"); 其余键缺失即违约。

CONTRACTS: Dict[str, Dict] = {
    # ---- 第二章精确两项 (items) ----
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
    # ---- 三~七节 (sections) ----
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

# ---------------- 机检判据 (确定式, 零 LLM) ----------------

_YEAR = re.compile(r"(?:19|20)\d{2}")


def _n_rows(table: Dict) -> int:
    rows = table.get("rows")
    return len(rows) if isinstance(rows, list) else 0


def _max_rows(extracted: Dict) -> int:
    return max((_n_rows(t) for t in extracted.get("tables") or []), default=0)


def _all_text(extracted: Dict) -> str:
    """text + 表格表头与全部单元格 — 关键词/年份常只在表格里
    (text 由 _clean_text 剥离了表格, 实证: 平安银行 nonrecurring '合计'在行内)"""
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
    """验收探针词: 与 parse.SECTION_RULES 切法词表同源 (单一事实源)"""
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
    """契约机检入口。kind: 'item'|'section'; extracted 为 _items.json 中该键的产物
    (或 None=缺失)。返回 {ok, reason} — 修复 Agent 与批量巡检共用本判据。"""
    if key not in CONTRACTS:
        return {"ok": False, "reason": f"未知契约键: {key}"}
    ok, reason = (_check_item if kind == "item" else _check_section)(key, extracted)
    return {"ok": ok, "reason": reason}


# ---------------- 修复 Agent 任务简报 ----------------

def repair_brief(kind: str, key: str) -> str:
    """契约 → Agent prompt 片段: 告诉修复 Agent 在找什么、什么算合格。"""
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


# ---------------- 批量合规巡检 ----------------

def check_data(d: Dict) -> Dict:
    """内存态 items 产物 → {fails: {key: reason}, n_keys, n_ok} (修复回环复检用)"""
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
    """单个 _items.json → {file, fails: {key: reason}, n_keys, n_ok}"""
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
