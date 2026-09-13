# -*- encoding: utf-8 -*-
"""
年报三~七节 L1 小节块提取入库: fin.annual_chunk

取舍 (2026-09-13 实证审核, 澜起/药明两样本; 上市公司画像视角):
- 第三节 MD&A     全量 (用户决策: 全部重要)
- 第四节 治理      关键词: 董监高/员工/股权激励/审计委员会 — 员工结构(澜起式在董监高
                  子节、药明式独立小节, 双覆盖); 治理概况/独立性/内控/环保不入
- 第五节 重要事项  关键词: 募集资金/募投/承诺/关联交易/担保/诉讼/退市/非标 (产能前瞻+风险簇)
- 第六节 股东      整节 (仅7-8k: 户数/限售/质押/控股股东; 节首前文单存 sub_order=0)
- 第七节 债券      整节但 <500 字跳过 (多数公司"不适用", 澜起实证 0k)

复用 sec2 的表格解析与 annual_core 的切片基础; 同一候选链 (年报→半年报降级)。

用法:
    python -m skills.content.annual_sections 澜起科技 [--year 2025] [--force] [--dry-run]
    python -m skills.content.annual_sections --index 000016.SH
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from core.logger import get_logger
from skills.content.annual_core import (
    _subsection_with_children, split_sections, split_subsections,
)
from skills.content.annual_report import _locate_report_pdfs
from skills.content.annual_sec2 import parse_md_tables

logger = get_logger(__name__)

MD_ROOT = Path("output/cninfo/md")

# 各节取舍: mode=all 全量 / keyword 按 L1 标题关键词命中;
# include_head=节首前文单存; skip_short=节正文过短跳过 (bonds 不适用属常态)
_RULES: Dict[str, Dict] = {
    "mdna":         {"mode": "all"},
    "governance":   {"mode": "keyword", "keywords": ["董监高", "董事和高级管理人员",
                                                     "员工", "股权激励", "审计委员会"]},
    "matters":      {"mode": "keyword", "keywords": ["募集资金", "募投", "承诺",
                                                     "关联交易", "担保", "诉讼",
                                                     "退市", "非标"]},
    "shareholders": {"mode": "all", "include_head": True},
    "bonds":        {"mode": "all", "skip_short": 500},
}


def extract_chunks(md: str) -> List[Dict]:
    """按 _RULES 提取各节 L1 小节块。返回 [{section_key, sub_order, title,
    text, tables}]。"""
    sections = split_sections(md)
    out: List[Dict] = []

    for section_key, rule in _RULES.items():
        text = sections.get(section_key, "")
        if not text:
            continue
        if rule.get("skip_short") and len(text.strip()) < rule["skip_short"]:
            continue

        subs = split_subsections(text)
        if rule.get("include_head"):
            # 节首前文 (第一个 L1 小节之前): 第六节股东总数/股本变动所在
            head = text[:_first_sub_offset(text)]
            if head.strip():
                out.append({"section_key": section_key, "sub_order": 0,
                            "title": "(节首前文)",
                            "text": head.strip(),
                            "tables": parse_md_tables(head)})

        for idx, (title, _, level) in enumerate(subs):
            if level != 1:
                continue
            if rule["mode"] == "keyword":
                # 澜起式: 员工情况是"五、董监高"的 L2 子节 → 由父小节整体带入
                if not any(kw in title for kw in rule["keywords"]):
                    continue
            body = _subsection_with_children(subs, idx)
            out.append({"section_key": section_key,
                        "sub_order": _l1_order(subs, idx),
                        "title": title,
                        "text": f"{title}\n{body.strip()}",
                        "tables": parse_md_tables(body)})
    return out


def _first_sub_offset(text: str) -> int:
    """节内第一个小节标题的偏移 (节首前文的长度)"""
    from skills.content.annual_core import _SUBSECTION_RE
    m = _SUBSECTION_RE.search(text)
    return m.start() if m else len(text)


def _l1_order(subs, idx: int) -> int:
    """L1 小节在节内的顺序号 (1 起, 不数 L2)"""
    n = 0
    for _, _, level in subs[:idx + 1]:
        if level == 1:
            n += 1
    return n


def build_sections(stock: str, year: Optional[int] = None,
                   force: bool = False, dry_run: bool = False) -> Dict:
    """单股三~七节块提取入库。"""
    from storage.pg import PgClient
    from skills.fin_query.skill import lookup_ts_code

    ts_code = lookup_ts_code(stock.strip())
    if not ts_code:
        return {"ok": False, "error": f"未识别股票: {stock}"}

    with PgClient() as pg:
        if not force:
            row = pg.fetch_one(
                "SELECT report_year FROM fin.annual_chunk "
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
        if loc is None:
            return {"ok": False, "error": f"{ts_code} 全部候选报告无文本层"}

        MD_ROOT.mkdir(parents=True, exist_ok=True)
        md_path = MD_ROOT / ts_code / f"{loc['report_year']}_{loc['category']}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        if not md_path.exists():
            md_path.write_text(md, encoding="utf-8")

        chunks = extract_chunks(md)
        if dry_run:
            by_sec: Dict[str, int] = {}
            for c in chunks:
                by_sec[c["section_key"]] = by_sec.get(c["section_key"], 0) + 1
            return {"ok": True, "dry_run": True, "ts_code": ts_code,
                    "report_year": loc["report_year"],
                    "chunks": len(chunks), "by_section": by_sec,
                    "titles": [f"{c['section_key']}.{c['sub_order']} {c['title'][:18]}"
                               for c in chunks]}

        if not chunks:
            return {"ok": False, "error":
                    f"{ts_code} {loc['report_year']} 三~七节块提取为空 (疑结构异常)"}

        for c in chunks:
            pg.execute(
                """
                INSERT INTO fin.annual_chunk
                    (ts_code, report_year, category, section_key, sub_order,
                     title, text, tables, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (ts_code, report_year, section_key, sub_order)
                DO UPDATE SET category=EXCLUDED.category, title=EXCLUDED.title,
                    text=EXCLUDED.text, tables=EXCLUDED.tables, updated_at=now()
                """,
                (ts_code, loc["report_year"], loc["category"],
                 c["section_key"], c["sub_order"], c["title"], c["text"],
                 json.dumps(c["tables"], ensure_ascii=False)))
        pg.conn.commit()
        by_sec: Dict[str, int] = {}
        for c in chunks:
            by_sec[c["section_key"]] = by_sec.get(c["section_key"], 0) + 1
        logger.info(f"三~七节块入库: {ts_code} {loc['report_year']} "
                    f"{len(chunks)} 块 {by_sec}")
        return {"ok": True, "ts_code": ts_code,
                "report_year": loc["report_year"],
                "chunks": len(chunks), "by_section": by_sec}


def build_for_index(index_code: str, force: bool = False) -> Dict[str, int]:
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
    logger.info(f"三~七节块批量: {index_code} {len(codes)} 只")
    stats = {"ok": 0, "skip": 0, "err": 0}
    for i, code in enumerate(codes, 1):
        r = build_sections(code, force=force)
        k = "skip" if r.get("skipped") else ("ok" if r["ok"] else "err")
        stats[k] += 1
        if not r["ok"]:
            logger.warning(f"[{i}/{len(codes)}] {code} 失败: {r['error']}")
        elif not r.get("skipped", False):
            logger.info(f"[{i}/{len(codes)}] {code} {r.get('chunks')} 块入库")
        time.sleep(1)
    return stats


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="年报三~七节 L1 小节块提取入库")
    p.add_argument("stock", nargs="?", default="", help="股票名或代码")
    p.add_argument("--year", type=int, default=0, help="指定报告年度")
    p.add_argument("--force", action="store_true", help="已入库也重提")
    p.add_argument("--dry-run", action="store_true", help="只提取不入库")
    p.add_argument("--index", default="", help="指数成分批量 (如 000016.SH)")
    args = p.parse_args()
    if args.index:
        stats = build_for_index(args.index, force=args.force)
        print(f"[SECTIONS] {stats}")
        return 0 if stats["err"] == 0 else 1
    if not args.stock:
        p.error("stock 或 --index 至少一个")
    r = build_sections(args.stock, year=args.year or None,
                       force=args.force, dry_run=args.dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
