# -*- encoding: utf-8 -*-
"""
T03 · 年报细粒度证据抽取 (Stage 1 事实层)

输入: output/cninfo/md/<ts_code>/<年>_ndbg_items.json
      (skills/content/annual_items.py 的产物, 本模块只读不写; sections 每小节含 title/text/tables)
输出: EvidenceShard(kind="annual") → output/research/<ts_code>/facts_annual_<年>.json
      + 人工抽检复核清单 annual_review_<年>.md (随机 10% 抽取句↔原句对照)

四类原子 (category, 登记于 shard.meta["fact_categories"], contracts.Fact 无此字段故不入 Fact):
  operating  经营数据数字证据 (产能/产量/销量/利用率/经销商数…, 财务报表之外最有价值)
  segment    分部/产品明细 (分产品/地区/渠道的收入、成本、毛利率)
  statement  管理层论断 (展望/战略/风险应对的原句引用, 不冒充数值事实)
  risk       风险条目

纪律 (对应任务书与 T00 铁律): 只抽原文明确陈述、禁止推断、数字原样照抄、
每条带小节锚点回溯 (source=《年报》·节·小节标题)、原文直引 confidence=1.0、换算 0.9 且注明公式。
eid 由本模块生成 (A:<年>.<节>.<小节序号>.<序号>), 绝不信任 LLM 生成 ID。

CLI: python -m skills.research.sources.annual_source 600519.SH 2025
"""
from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger
from core.paths import PathManager
from harness.llm import get_extract_llm
from skills.research.contracts import (
    EvidenceShard,
    Fact,
    research_dir,
    save_shard,
    validate_shard,
    valid_period,
)

logger = get_logger(__name__)

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
# 空小节阈值: 正文过短且无表格时不值得一次 LLM 调用
_MIN_BODY_CHARS = 30
# 契约兜底: content 含数字但 value/unit 均空时, 从数字后紧跟的单位抢救 (长词优先;
# 须紧邻数字, 防"发达国【家】""两【个】方向"之类误配)
_UNIT_ADJACENT_RE = re.compile(
    r"\d(?:,\d{3})*(?:\.\d+)?\s*(个百分点|万吨|千吨|亿元|万元|亿|万|吨|%|元|家|人|个|项|次|名|强|款|瓶|户|亩|度|千瓦时)"
)
_HAS_DIGIT = re.compile(r"\d")

# 系统提示词为普通字符串 (非 .format/f-string 模板), JSON 花括号按字面量直书
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


# ---------- 输入与序列化 ----------

def annual_items_path(ts_code: str, year: int) -> Path:
    """output/cninfo/md/<ts_code>/<年>_ndbg_items.json"""
    return Path(PathManager().output_dir) / "cninfo" / "md" / ts_code / f"{year}_ndbg_items.json"


def load_annual_items(items_path: Path) -> Dict[str, Any]:
    if not items_path.exists():
        raise FileNotFoundError(
            f"年报 items 不存在: {items_path} (先运行 skills/content/annual_items.py 生成)")
    data = json.loads(items_path.read_text(encoding="utf-8"))
    if not isinstance(data.get("sections"), dict):
        raise ValueError(f"items 缺少 sections 结构: {items_path}")
    return data


def _table_to_lines(table: Dict[str, Any]) -> List[str]:
    """表格 → 管道分隔文本行 (表名/单位并入首行, 供 LLM 保留单位口径)"""
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
    """小节正文 + 表格 → 送入 LLM 的原文块"""
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


# ---------- LLM 调用与宽容解析 ----------

def extract_subsection(llm, year: int, sec_key: str, title: str, body: str) -> List[Dict[str, Any]]:
    """单小节一次 LLM 调用 → 原始原子数组 (未清洗); 解析失败抛 ValueError"""
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
    """混杂文本中剥最外层 JSON 容器: 以先出现的 [ 或 { 为界, 取到与之配对的最后一个闭括号"""
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
    """宽容解析: 剥 ```json 围栏; 接受 {"facts":[...]} 包装或裸数组; 彻底失败返回 None"""
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


# ---------- 清洗与契约化 ----------

def _to_float(v: Any) -> Optional[float]:
    """value 宽容数值化: 千分位/百分号/带单位串 → 纯 float; 不可数值化返回 None"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("，", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _norm_period(p: Any, year: int) -> str:
    """period 归一: 年报口径只允许 {年}A (流量) / {年}-12-31 (期末存量) / 合法原值"""
    s = str(p or "").strip()
    flow = ("", str(year), f"{year}年", f"{year}年度", f"{year}A", "报告期", "本期")
    stock = (f"{year}年末", f"{year}-12-31", "期末", f"{year}年期末")
    if s in flow:
        return f"{year}A"
    if s in stock:
        return f"{year}-12-31"
    return s if valid_period(s) else f"{year}A"


def _sanitize_fact(raw: Dict[str, Any], year: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """LLM 原始条目 → 干净条目; 返回 (条目, 丢弃原因) — 条目为 None 即丢弃"""
    category = str(raw.get("category") or "").strip()
    if category not in CATEGORIES:
        return None, "bad_category"
    content = re.sub(r"\s+", " ", str(raw.get("content") or "")).strip()
    if not content:
        return None, "empty_content"
    value = _to_float(raw.get("value"))
    unit = str(raw.get("unit") or "").strip()
    if value is None and not unit and _HAS_DIGIT.search(content):
        m = _UNIT_ADJACENT_RE.search(content)      # 契约兜底: 从数字后抢救单位
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


def _enforce_contract(shard: EvidenceShard, dropped: List[Dict[str, Any]]) -> None:
    """契约兜底: validate_shard 报错的 fact 逐条剔除 (sanitize 后仍违规的残余)"""
    for _ in range(200):                          # 防御性上限
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


# ---------- 复核清单 ----------

def write_review_md(path: Path, shard: EvidenceShard, year: int, seed: str,
                    sample_size: int) -> List[str]:
    """随机 10% 抽检清单: 抽取句 ↔ 原句 对照 (质量承诺的可审计形式)。
    原句存于 shard.meta["quotes"] (Fact 契约无 quote 字段, 不入 Fact)。"""
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


# ---------- 主流程 ----------

def collect_annual_facts(ts_code: str, year: int, out_dir: Path | None = None,
                         items_path: Path | None = None) -> EvidenceShard:
    """年报 items → 原子级证据分片 (facts_annual_<年>.json + annual_review_<年>.md)"""
    items_path = items_path or annual_items_path(ts_code, year)
    data = load_annual_items(items_path)
    sections: Dict[str, Any] = data["sections"]

    shard = EvidenceShard(kind="annual", ts_code=ts_code)
    cat_of: Dict[str, str] = {}
    quotes: Dict[str, str] = {}
    dropped: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    section_stats: Dict[str, Dict[str, int]] = {}
    llm = None                                       # 惰性创建: 全部小节为空时不碰 LLM

    for sec_key in SECTION_ORDER:
        subs = (sections.get(sec_key) or {}).get("subsections") or []
        stat = {"subsections": len(subs), "facts": 0, "empty": 0, "llm_failed": 0}
        for sub_idx, sub in enumerate(subs, 1):
            title = str(sub.get("title") or f"小节{sub_idx}").strip()
            body = build_subsection_body(sub)
            if len(body) < _MIN_BODY_CHARS:
                stat["empty"] += 1
                skipped.append({"section": sec_key, "title": title, "reason": "empty"})
                continue
            try:
                if llm is None:
                    # 300s: 思考模式下大经营小节(正文+20余张表)单次超 120s 默认超时
                    llm = get_extract_llm(timeout=300.0)
                raws = extract_subsection(llm, year, sec_key, title, body)
            except Exception as e:                  # 单小节失败不中断整体
                stat["llm_failed"] += 1
                skipped.append({"section": sec_key, "title": title,
                                "reason": f"llm_error: {type(e).__name__}: {e}"})
                logger.warning("小节抽取失败跳过 %s·%s: %s", sec_key, title, e)
                continue

            seen_content = set()
            sub_seq = 0                                # eid 末段: 小节内序号 (每小节重新计数)
            for raw in raws:
                clean, reason = _sanitize_fact(raw, year)
                if clean is None:
                    dropped.append({"reason": reason, "sample": str(raw.get("content"))[:50]})
                    continue
                if clean["content"] in seen_content:     # 同小节内容去重
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
    alive = {f.eid for f in shard.facts}             # 剔除后 meta 侧同步收敛
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
