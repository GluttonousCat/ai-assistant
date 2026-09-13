# -*- encoding: utf-8 -*-
"""
年报第二节核心子章节提取入库: fin.annual_sec2

提取两个子章节 (表格 + 指标说明文字, 全确定性零 LLM):
- kpi3y       近三年主要会计数据和财务指标 (主要会计数据表/主要财务指标表/变动说明)
- nonrecurring 非经常性损益项目和金额 (明细表+说明) — 盈利质量分析的弹药

分季度数据不入库: Tushare fin.income 四报告期累计可推导单季
(2026-09-13 澜起验证: 年报全年营收 5,456,316,783.63 与 fin.income 20251231 逐位一致)。

复用 annual_core 的确定性切片基础 (split_sections/split_subsections/
_subsection_with_children — 节按名匹配、小节两级切分、L1 自动并入 L2 子节)。

用法:
    python -m skills.content.annual_sec2 澜起科技 [--year 2025] [--force] [--dry-run]
    python -m skills.content.annual_sec2 --index 000016.SH      # 指数批量
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from core.logger import get_logger
from skills.content.annual_core import (
    _subsection_with_children, split_sections, split_subsections,
)
from skills.content.annual_report import _locate_report_pdfs

logger = get_logger(__name__)

MD_ROOT = Path("output/cninfo/md")

# 子章节标题关键词 → 入库键 (编号各司不同: 澜起六/九 药明七/十, 按名匹配)
_SEC2_TARGETS = [
    ("kpi3y", "近三年主要会计数据"),
    ("nonrecurring", "非经常性损益项目和金额"),
]

_ROW_RE = re.compile(r"^\|.+\|\s*$")
_SEP_RE = re.compile(r"^\|[\s:|-]+\|\s*$")


def _clean_cell(cell: str) -> str:
    """单元格清洗: 去首尾空白与包裹符; <br> 是 pymupdf4llm 的折行产物, 去除后
    跨行词自然接合 (实证: '归属于上市公司股<br>东的净利润')。"""
    return cell.strip().strip("|").replace("<br>", "").strip()


def parse_md_tables(text: str) -> List[Dict]:
    """解析 MD 管道表格块。返回 [{"header": [...], "rows": [[...], ...]}]。

    规则 (实证 澜起2025 L80-92):
    - 连续 |..| 行为一块; 分隔行 |---| 的上一行是表头, 其后到下一分隔行为数据
    - 一块内多个分隔行 → 拆多表 (拼接无空行的连续表)
    - 内嵌子表头行 (如 '||2025年末|2024年末|…' 首列空+年份列) 保留为数据行 —
      它承载口径切换信息 (年度数据→年末数据), 下游可识别
    """
    tables: List[Dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not _ROW_RE.match(lines[i]):
            i += 1
            continue
        block: List[str] = []
        while i < len(lines) and _ROW_RE.match(lines[i]):
            block.append(lines[i])
            i += 1
        cells = [[_clean_cell(c) for c in row.split("|")[1:-1]]
                 for row in block]

        current: Optional[Dict] = None
        for j, row in enumerate(cells):
            is_sep = row and all(set(c) <= set("-: ") and c for c in row)
            if is_sep:
                if current and current["rows"]:
                    tables.append(current)
                current = {"header": cells[j - 1] if j else [],
                           "rows": []}
            elif current is not None:
                if any(c for c in row):          # 跳过全空行
                    current["rows"].append(row)
        if current and current["rows"]:
            tables.append(current)
    return tables


def extract_sec2(md: str) -> Dict[str, Dict]:
    """从全文 MD 提取第二节目标子章节。返回 {key: {title, text, tables}}。

    命中不到的键不出现在结果里 (miss 容忍, 由调用方决定是否告警)。
    """
    summary = split_sections(md).get("summary", "")
    subs = split_subsections(summary)
    out: Dict[str, Dict] = {}
    for key, kw in _SEC2_TARGETS:
        for idx, (title, _, level) in enumerate(subs):
            if kw in title and level == 1:
                # L1 全文 (含 (一)(二) 子小节的表格 + 父小节尾部的指标说明文字)
                body = _subsection_with_children(subs, idx)
                text = f"{title}\n{body.strip()}"
                out[key] = {"title": title, "text": text,
                            "tables": parse_md_tables(body)}
                break
    return out


def build_sec2(stock: str, year: Optional[int] = None,
               force: bool = False, dry_run: bool = False) -> Dict:
    """单股第二节提取入库。返回 {ok, ts_code, report_year, extracted: {key: 表数/字数}}。"""
    from storage.pg import PgClient
    from skills.fin_query.skill import lookup_ts_code

    ts_code = lookup_ts_code(stock.strip())
    if not ts_code:
        return {"ok": False, "error": f"未识别股票: {stock}"}

    with PgClient() as pg:
        if not force:
            row = pg.fetch_one(
                "SELECT report_year FROM fin.annual_sec2 "
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

        # MD 落盘 (与 annual_core 同一约定, L0 溯源)
        MD_ROOT.mkdir(parents=True, exist_ok=True)
        md_path = MD_ROOT / ts_code / f"{loc['report_year']}_{loc['category']}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        if not md_path.exists():
            md_path.write_text(md, encoding="utf-8")

        result = extract_sec2(md)
        if dry_run:
            return {"ok": True, "dry_run": True, "ts_code": ts_code,
                    "report_year": loc["report_year"],
                    "extracted": {k: {"title": v["title"],
                                      "chars": len(v["text"]),
                                      "tables": len(v["tables"]),
                                      "rows": [len(t["rows"])
                                               for t in v["tables"]]}
                                  for k, v in result.items()}}

        if not result:
            return {"ok": False, "error":
                    f"{ts_code} {loc['report_year']} 第二节目标子章节未命中 "
                    f"(疑结构异常, 值得人工看 {md_path})"}

        for key, v in result.items():
            pg.execute(
                """
                INSERT INTO fin.annual_sec2
                    (ts_code, report_year, category, subsection_key,
                     title, text, tables, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (ts_code, report_year, subsection_key) DO UPDATE SET
                    category=EXCLUDED.category, title=EXCLUDED.title,
                    text=EXCLUDED.text, tables=EXCLUDED.tables, updated_at=now()
                """,
                (ts_code, loc["report_year"], loc["category"], key,
                 v["title"], v["text"],
                 json.dumps(v["tables"], ensure_ascii=False)))
        pg.conn.commit()
        summary_ = {k: {"tables": len(v["tables"]),
                        "chars": len(v["text"])} for k, v in result.items()}
        logger.info(f"第二节核心入库: {ts_code} {loc['report_year']} {summary_}")
        return {"ok": True, "ts_code": ts_code,
                "report_year": loc["report_year"], "extracted": summary_}


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
    logger.info(f"第二节核心批量: {index_code} {len(codes)} 只")
    stats = {"ok": 0, "skip": 0, "err": 0}
    for i, code in enumerate(codes, 1):
        r = build_sec2(code, force=force)
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
    p = argparse.ArgumentParser(description="年报第二节核心子章节提取入库")
    p.add_argument("stock", nargs="?", default="", help="股票名或代码")
    p.add_argument("--year", type=int, default=0, help="指定报告年度")
    p.add_argument("--force", action="store_true", help="已入库也重提")
    p.add_argument("--dry-run", action="store_true", help="只提取不入库不落盘")
    p.add_argument("--index", default="", help="指数成分批量 (如 000016.SH)")
    args = p.parse_args()
    if args.index:
        stats = build_for_index(args.index, force=args.force)
        print(f"[SEC2] {stats}")
        return 0 if stats["err"] == 0 else 1
    if not args.stock:
        p.error("stock 或 --index 至少一个")
    r = build_sec2(args.stock, year=args.year or None,
                   force=args.force, dry_run=args.dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
