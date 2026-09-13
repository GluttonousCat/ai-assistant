# -*- encoding: utf-8 -*-
"""
年报核心信息提取入库 (v2, 实证驱动设计)

完整画像 = Tushare 财务信息(已有, 不复制) ⊕ 年报核心信息(本模块产出, 入
fin.annual_report_core)。定性/数字两层分抽: 数字层正则先行确定性提取
(LLM 不经手), 定性层 LLM 在切片语料上提炼。

实证依据 (2026-09-11 通读澜起688008/药明603259 两份 2025 年报 MD, ~27万字/份):
- 节锚点 `^#{1,4} 第X节` 两家均成立; 但节序/合并不统一 (两家均"治理+环境"合并、
  "股份变动"在第六节), 故按节名匹配不按序号
- 风险节措辞变体: 澜起"四、风险因素" vs 药明"(四)可能面对的风险" (澜起L1833)
- 募投/募集资金在第五节重要事项 (非第三节; 两家实证)
- 审计意见最早出现在第二节前的"重要提示"区一行 (澜起L99)
- 研发人员数双落点: 第三节研发投入小节 + 第四节员工情况 (两家同)
- 在手订单/产能利用率为行业性披露 (药明有, 澜起无) → 数字层自由槽

用法:
    python -m skills.content.annual_core 澜起科技 [--year 2025] [--force] [--dry-run]
    python -m skills.content.annual_core --index 000016.SH        # 批量
    --dry-run: 只出切片+数字层不调 LLM (审核/调试中间产物)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger
from skills.content.annual_report import _locate_report_pdfs

logger = get_logger(__name__)

MD_ROOT = Path("output/cninfo/md")

# ---------------- 节层 ----------------

# 节名 → 内部键 (按名匹配不按序号; 实证: 各家合并节方式不一)
_SECTION_KEYS = [
    ("管理层讨论", "mdna"),
    ("公司治理", "governance"),       # 含合并形态"公司治理、环境和社会"
    ("重要事项", "matters"),
    ("股份变动", "shareholders"),
    ("财务报告", "financial"),
    ("公司简介", "summary"),
    ("释义", "definitions"),
    ("债券", "bonds"),
]

_SECTION_RE = re.compile(r"^#{1,4}\s*第[一二三四五六七八九十]+节\s*(.*)$",
                         re.M)


def _section_key(title: str) -> str:
    for kw, key in _SECTION_KEYS:
        if kw in title:
            return key
    return "other"


def split_sections(md: str) -> Dict[str, str]:
    """按「第X节」标题切节。返回 {内部键: 节内文本} (同名键后者覆盖, 实际不重现)。"""
    out: Dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(md))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out[_section_key(m.group(1))] = md[m.end():end]
    return out


def slice_head(md: str) -> str:
    """重要提示区: 全文头到第一个「第X节」(审计意见/利润分配预案在此, 实证澜起L99)。"""
    m = _SECTION_RE.search(md)
    return md[:m.start()] if m else md[:3000]


# ---------------- 小节层 ----------------

# 小节标题三形态: "一、"(一级) / "（一）"(二级) / "(四)"(二级, 药明实证)
# 层级必须区分: 澜起"四、风险因素"正文仅54字, 风险内容全在(一)(二)子小节 (实证L1833+)
# # 前缀可选: 药明后半部分小节标题在 MD 里是纯文本行 (pymupdf4llm 未识别为标题,
#   实证: '十四、募集资金使用进展说明' 无 # 前缀) — 靠短行守卫防误切:
#   标题行 ≤50 字且不以句读结尾, 叙述段落(长行/带句号)不会命中
_SUBSECTION_RE = re.compile(
    r"^(?P<hash>#{2,4})?\s*(?P<num>[（(]?[一二三四五六七八九十]+[）)]?[、．.]?)\s*"
    r"(?P<title>[^\n]{1,50})$", re.M)
_LEVEL1 = re.compile(r"[一二三四五六七八九十]+、")


def split_subsections(section_text: str) -> List[Tuple[str, str, int]]:
    """节内按「一、/（一）/(四)」小节切, 返回 [(标题, 正文, 层级1|2)]。

    标题两级兼容: 药明 ### / 澜起 #### (实证); 一级='一、' 形态, 二级='（一）'形态。
    """
    out: List[Tuple[str, str, int]] = []
    matches = list(_SUBSECTION_RE.finditer(section_text))
    for i, m in enumerate(matches):
        # 无 # 前缀的纯文本标题行: 追加句读守卫 (标题不以句号/分号/冒号结尾)
        if not m.group("hash"):
            title_line = m.group(0).strip()
            if title_line.endswith(("。", "；", "，", "：", ":", "；")):
                continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        level = 1 if _LEVEL1.fullmatch(m.group("num")) else 2
        out.append((m.group("title").strip(),
                    section_text[m.end():end], level))
    return out


def _subsection_with_children(subs: List[Tuple[str, str, int]],
                              idx: int) -> str:
    """选中 subs[idx] 的正文; 若为一级小节, 并入其后连续的二级子小节
    (澜起式: '四、风险因素' 的内容全在 (一)(二)… 子小节里)。"""
    title, body, level = subs[idx]
    parts = [body]
    if level == 1:
        for t, b, lv in subs[idx + 1:]:
            if lv == 1:
                break
            parts.append(f"\n[{t}]\n" + b)
    return "".join(parts)


# MD&A 内保的小节 (按序填充语料预算; 实证: 措辞变体在两家均命中)
_MDNA_PREFER = [
    ("经营情况讨论", "mdna"),
    ("主要经营情况", "mdna"),
    ("主营业务分析", "mdna"),
    ("风险因素", "mdna"),
    ("可能面对的风险", "mdna"),
    ("主要子公司", "mdna"),
    ("研发投入", "mdna"),
]

# ---------------- 数字层 (正则确定性, LLM 不经手) ----------------

def _num(s: str) -> Optional[int]:
    """'1,234'/'1，234' → 1234"""
    d = re.sub(r"[,，\s]", "", s)
    return int(d) if d.isdigit() else None


def q_audit_opinion(head: str) -> Optional[str]:
    """审计意见 — 重要提示区一行可得 (实证: 澜起L99 '标准无保留意见的审计报告')"""
    m = re.search(r"(标准无保留|带强调事项段的无保留|保留|无法表示|否定)意见", head)
    return m.group(1) + "意见" if m else None


def q_holder_count(shareholders: str) -> Optional[int]:
    """股东户数 — 股东节 (实证: 澜起L4353/药明L2722 '股东总数 XX 户')"""
    m = re.search(r"股东总数[^0-9]{0,15}([\d,，]+)\s*户", shareholders)
    return _num(m.group(1)) if m else None


def q_staff_total(governance: str) -> Optional[int]:
    """员工总数 — 治理节员工情况小节 (实证: 澜起L3131/药明L1717)"""
    m = re.search(r"在职员工的数量合计[^0-9]{0,10}([\d,，]+)", governance)
    return _num(m.group(1)) if m else None


def q_r_and_d(mdna: str, governance: str) -> Optional[Dict]:
    """研发人员 — 双落点扫描: MD&A研发投入小节 + 治理节员工情况 (两家实证)"""
    corpus = mdna + "\n" + governance
    m = re.search(r"研发人员数量[^0-9]{0,10}([\d,，]+)", corpus)
    if not m:
        return None
    pct = re.search(r"研发人员[^。]{0,20}?占比[^0-9]{0,8}([\d.]+)\s*[%％]", corpus)
    return {"count": _num(m.group(1)),
            "ratio_pct": float(pct.group(1)) if pct else None}


def q_top5(mdna: str) -> Optional[Dict]:
    """前五大客户/供应商占比 — MD&A (实证: 澜起L2147/药明L645 '前五名客户…占…X%')"""
    def _one(word: str) -> Optional[float]:
        m = re.search(rf"前五名?{word}[^。％%]{{0,80}}?([\d.]+)\s*[%％]", mdna)
        return float(m.group(1)) if m else None
    cust, supp = _one("客户"), _one("供应商")
    if cust is None and supp is None:
        return None
    return {"top5_customers_pct": cust, "top5_suppliers_pct": supp}


def q_forward_indicators(mdna: str) -> Dict:
    """行业前瞻指标自由槽 (有则填): 在手订单/产能利用率 (实证: 药明L403/554, 澜起无)"""
    out = {}
    m = re.search(r"在手订单[^。]{0,30}?([\d.]+)\s*亿元", mdna)
    if m:
        out["order_backlog_yi"] = float(m.group(1))
    m = re.search(r"产能利用率[^。％%]{0,20}?([\d.]+)\s*[%％]", mdna)
    if m:
        out["capacity_utilization_pct"] = float(m.group(1))
    return out


def extract_quant(md: str, sections: Dict[str, str]) -> Dict:
    """数字层总装。单字段失败存 null 不阻塞 (miss 容忍)。"""
    head = slice_head(md)
    mdna = sections.get("mdna", "")
    return {
        "audit_opinion": q_audit_opinion(head),
        "holder_count": q_holder_count(sections.get("shareholders", "")),
        "staff_total": q_staff_total(sections.get("governance", "")),
        "r_and_d": q_r_and_d(mdna, sections.get("governance", "")),
        "top5": q_top5(mdna),
        "forward_indicators": q_forward_indicators(mdna),
    }


# ---------------- 语料组装 ----------------

_CORPUS_BUDGET_MDNA = 15000

def build_corpus(md: str, sections: Dict[str, str]
                 ) -> Tuple[str, List[Dict]]:
    """组装 LLM 语料 (≈2万字) + 使用清单 (可解释性)。

    构成: 重要提示区 1k + MD&A 核心小节 15k + 治理·员工小节 1.5k
          + 重要事项·募投小节 1.5k + 股东节前段 0.5k
    """
    used: List[Dict] = []

    def _take(source: str, title: str, text: str, cap: int) -> str:
        text = text.strip()[:cap]
        used.append({"source": source, "title": title, "chars": len(text)})
        return text

    parts: List[str] = []

    head = slice_head(md)
    parts.append("## 重要提示\n" + _take("head", "重要提示", head, 1000))

    # MD&A 核心小节: 按优先序填预算 (标题随片入语料; L1 自动并入 L2 子节)
    budget = _CORPUS_BUDGET_MDNA
    taken_idx = set()
    subs = split_subsections(sections.get("mdna", ""))
    for kw, _ in _MDNA_PREFER:
        if budget <= 0:
            break
        for idx, (title, _, __) in enumerate(subs):
            if idx in taken_idx or kw not in title:
                continue
            piece = _subsection_with_children(subs, idx)[:budget]
            parts.append(f"### {title}\n" + piece)
            used.append({"source": "mdna", "title": title,
                         "chars": len(piece)})
            taken_idx.add(idx)
            budget -= len(piece)
            break

    # 治理节·员工小节
    for title, body, _ in split_subsections(sections.get("governance", "")):
        if "员工" in title:
            parts.append("## 员工情况\n" + _take("governance", title,
                                                 f"{title}\n{body.strip()}", 1500))
            break

    # 重要事项·募投小节 (实证: 募集资金使用在第五节)
    for title, body, _ in split_subsections(sections.get("matters", "")):
        if "募集资金" in title or "募投" in title:
            parts.append("## 募集资金\n" + _take("matters", title,
                                                 f"{title}\n{body.strip()}", 1500))
            break

    # 股东节前段 (户数所在)
    sh = sections.get("shareholders", "")[:500]
    if sh:
        parts.append("## 股东情况(前段)\n" + _take("shareholders", "前段", sh, 500))

    return "\n\n".join(parts), used


# ---------------- 定性层 (LLM) ----------------

_EXTRACT_PROMPT = """你是投研分析师。以下是{name}({ts_code}) {label}的章节节选 \
(重要提示/管理层讨论/员工/募集资金/股东)。基于节选提炼「年报核心信息·定性层」, \
严格输出一个 JSON 对象:
- 所有文本一律简体中文; 数字/百分比照抄原文口径, 不换算不四舍五入
- 节选没有的字段填 null, 绝不编造; 列表字段给 3~6 条
- 产销量类信息必须引用原句: {{"item": "产品", "value": "原文数值+单位", "quote": "原句"}}

{{
  "digest": "300~500字要点: 一段业务与经营、一段亮点与隐忧、一段主要风险",
  "business_review": ["经营回顾: 主营业务进展/价格趋势/客户与市场结构"],
  "growth_drivers": ["管理层自述的增长动因"],
  "capex": ["资本开支与募投项目要点"],
  "strategy_outlook": ["战略与来年经营计划"],
  "risks": ["风险清单, 说人话"],
  "subsidiaries": ["主要子公司经营贡献"],
  "production_sales": [{{"item": "", "value": "", "quote": ""}}]
}}

节选:
{corpus}"""


def _parse_json_loose(text: str) -> Optional[Dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


# ---------------- 编排 ----------------

def build_annual_core(stock: str, year: Optional[int] = None,
                      force: bool = False, dry_run: bool = False) -> Dict:
    """单股年报核心信息构建入库。dry_run=True 只产中间产物 (切片+数字层) 不调 LLM。"""
    from storage.pg import PgClient
    from skills.fin_query.skill import lookup_ts_code

    ts_code = lookup_ts_code(stock.strip())
    if not ts_code:
        return {"ok": False, "error": f"未识别股票: {stock}"}

    with PgClient() as pg:
        if not force:
            row = pg.fetch_one(
                "SELECT report_year FROM fin.annual_report_core "
                "WHERE ts_code=%s AND (%s::int IS NULL OR report_year=%s) "
                "ORDER BY report_year DESC LIMIT 1", (ts_code, year, year))
            if row:
                return {"ok": True, "ts_code": ts_code, "skipped": True,
                        "report_year": row["report_year"]}

        cands = _locate_report_pdfs(ts_code, year)
        if not cands:
            return {"ok": False, "error":
                    f"{ts_code} 无已下载年报/半年报 PDF (先跑 tools.cninfo.downloader)"}

        from tools.pdf import to_markdown
        loc, md = None, ""
        for row_ in cands:
            if not Path(row_["file_path"]).exists():
                continue
            md = to_markdown(row_["file_path"])
            if len(md.strip()) >= 2000:
                loc = row_
                break
            logger.info(f"{ts_code} {row_['report_year']} {row_['category']} "
                        f"MD 过短 ({len(md)} 字), 试下一候选")
        if loc is None:
            return {"ok": False, "error":
                    f"{ts_code} 全部候选报告无文本层 (图片版需先 OCR)"}

        label = (f"{loc['report_year']} 年度报告" if loc["category"] == "ndbg"
                 else f"{loc['report_year']} 半年度报告")

        # MD 落盘 (供追问/问答)
        MD_ROOT.mkdir(parents=True, exist_ok=True)
        md_path = MD_ROOT / ts_code / f"{loc['report_year']}_{loc['category']}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")

        sections = split_sections(md)
        quant = extract_quant(md, sections)
        corpus, used = build_corpus(md, sections)

        if dry_run:
            return {"ok": True, "dry_run": True, "ts_code": ts_code,
                    "report_year": loc["report_year"], "label": label,
                    "md_chars": len(md),
                    "sections": {k: len(v) for k, v in sections.items()},
                    "quantitative": quant, "used_slices": used,
                    "corpus_chars": len(corpus)}

        if len(corpus) < 500:
            return {"ok": False, "error":
                    f"{ts_code} {label} 语料组装过短 ({len(corpus)} 字), 疑章节定位失败"}

        from core.llm.client import get_extract_llm
        llm = get_extract_llm(timeout=300.0)
        out = llm.invoke(_EXTRACT_PROMPT.format(
            name=stock, ts_code=ts_code, label=label, corpus=corpus))
        qual = _parse_json_loose(out)
        if not qual or not qual.get("digest"):
            return {"ok": False, "error": f"{ts_code} {label} 定性层解析失败"}

        pg.execute(
            """
            INSERT INTO fin.annual_report_core
                (ts_code, report_year, category, qualitative, quantitative,
                 digest, md_path, announcement_id, model, extracted_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())
            ON CONFLICT (ts_code, report_year) DO UPDATE SET
                category=EXCLUDED.category, qualitative=EXCLUDED.qualitative,
                quantitative=EXCLUDED.quantitative, digest=EXCLUDED.digest,
                md_path=EXCLUDED.md_path, announcement_id=EXCLUDED.announcement_id,
                model=EXCLUDED.model, extracted_at=now(), updated_at=now()
            """,
            (ts_code, loc["report_year"], loc["category"],
             json.dumps(qual, ensure_ascii=False),
             json.dumps(quant, ensure_ascii=False), qual["digest"],
             str(md_path.resolve()), loc.get("announcement_id"), llm.model))
        pg.conn.commit()
        n_used = len(used)
        logger.info(f"年报核心信息入库: {ts_code} {label} "
                    f"(语料 {len(corpus)} 字/{n_used} 片, digest {len(qual['digest'])} 字)")
        return {"ok": True, "ts_code": ts_code, "report_year": loc["report_year"],
                "label": label, "corpus_chars": len(corpus),
                "used_slices": n_used, "digest_chars": len(qual["digest"]),
                "quantitative": quant}


def build_for_index(index_code: str, force: bool = False,
                    dry_run: bool = False) -> Dict[str, int]:
    """指数成分股批量 (幂等跳过已入库)"""
    import time
    from datetime import date, timedelta
    import tushare as ts
    from core.config import get_config

    pro = ts.pro_api(get_config().tushare_token)
    start = (date.today() - timedelta(days=95)).strftime("%Y%m%d")
    df = pro.index_weight(index_code=index_code, start_date=start,
                          end_date=date.today().strftime("%Y%m%d"))
    latest = df["trade_date"].max()
    codes = sorted(df[df["trade_date"] == latest]["con_code"].unique())
    logger.info(f"年报核心信息批量: {index_code} {len(codes)} 只")
    stats = {"ok": 0, "skip": 0, "err": 0}
    for i, code in enumerate(codes, 1):
        r = build_annual_core(code, force=force, dry_run=dry_run)
        k = "skip" if r.get("skipped") else ("ok" if r["ok"] else "err")
        stats[k] += 1
        if not r["ok"]:
            logger.warning(f"[{i}/{len(codes)}] {code} 失败: {r['error']}")
        elif not r.get("skipped", False):
            logger.info(f"[{i}/{len(codes)}] {code} {r.get('report_year')} 入库")
        time.sleep(1)
    return stats


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="年报核心信息提取入库")
    p.add_argument("stock", nargs="?", default="", help="股票名或代码")
    p.add_argument("--year", type=int, default=0, help="指定报告年度")
    p.add_argument("--force", action="store_true", help="已入库也重提")
    p.add_argument("--dry-run", action="store_true",
                   help="只出切片+数字层, 不调 LLM (审核中间产物)")
    p.add_argument("--index", default="", help="指数成分批量 (如 000016.SH)")
    args = p.parse_args()
    if args.index:
        stats = build_for_index(args.index, force=args.force,
                                dry_run=args.dry_run)
        print(f"[ANNUAL-CORE] {stats}")
        return 0 if stats["err"] == 0 else 1
    if not args.stock:
        p.error("stock 或 --index 至少一个")
    r = build_annual_core(args.stock, year=args.year or None,
                          force=args.force, dry_run=args.dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
