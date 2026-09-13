# -*- encoding: utf-8 -*-
"""
年报披露项提取: 全文 MD → {item: {title, text, tables}} (纯文件产物, 不碰 SQL)

逻辑 (四步, 无方言判定/无节切分 — 披露项标题即锚点, 证监会准则规定跨排版稳定):
  ① 定位  独立短行命中词表别名 (守卫: 行≈别名, 排除叙述句)
  ② 切块  标题行 → 下一同级标记行 (编号种类判兄弟: 一、=兄弟, （一）=子内容)
  ③ 分离  块内管道行 → tables; 其余 → text
  ④ 清洗  标题去编号/装饰; text 去页码/表格/<br>; tables 单位行并表名

五条硬规则 (实证, 见各函数注释): 标题守卫/编号判兄弟/行尾rstrip/
短节接受(≥150字或含表)/词表按实证扩。

用法:
    python -m skills.content.annual_items 澜起科技 [--year 2025]
    → output/cninfo/md/<代码>/<年度>_<类别>_items.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger

logger = get_logger(__name__)

MD_ROOT = Path("output/cninfo/md")

# 披露项词表 (受控 item_key; 别名按实证扩充, 遇新措辞加一条)
ITEM_ALIASES: Dict[str, List[str]] = {
    "kpi3y": ["主要会计数据和财务指标", "主要财务数据及指标", "近三年主要会计数据"],
    "nonrecurring": ["非经常性损益项目和金额", "非经常性损益项目及涉及金额",
                     "非经常性损益项目"],
    "business_review": ["经营情况讨论与分析", "报告期内主要经营情况", "主营业务分析",
                        "经营业绩回顾"],
    "risks": ["可能面对的风险", "风险因素"],
    "top5": ["前五名客户", "前五大客户"],
    "staff": ["员工情况"],
    "capex": ["募集资金使用情况", "募集资金使用进展", "募集资金使用"],
    "strategy": ["未来发展的讨论与分析", "公司发展战略", "经营计划"],
    "subsidiaries": ["主要子公司情况", "主要控股参股公司"],
}

# ---------------- ① 定位与② 边界 ----------------

_L1_NUM = re.compile(r"^[一二三四五六七八九十]+、\S{0,50}$")
_L2_NUM = re.compile(r"^[（(][一二三四五六七八九十]+[）)]\s*\S{0,50}$")
_D_NUM = re.compile(r"^\d+(?:\.\d+)+\s*\S{0,50}$")
_HEAD = re.compile(r"^(#{1,6})\s+\S")
_PAGE_NO = re.compile(r"^\d+\s*/\s*\d+$")

_BARE_STRIP = re.compile(r"[#\s、。（(）)0-9.\-—第三节第四五六年度近<>*_/a-zA-Z]")


def marker_rank(line: str) -> Optional[float]:
    """行标记秩 (浅→深)。编号优先于#层级 — 实证澜起 L1(六、)与L2((一))同为####,
    #层数分不了兄弟, 必须按编号种类判。秩: #标题=层数; 一、=2.5; N.M=3.0; （一）=3.5"""
    line = line.rstrip()          # MD 行尾常带尾随空格, 会击穿 $ 锚点
    body = re.sub(r"^#{1,6}\s*", "", line)
    if _L1_NUM.match(body):
        return 2.5
    if _D_NUM.match(body):
        return 3.0
    if _L2_NUM.match(body):
        return 3.5
    m = _HEAD.match(line)
    if m:
        return float(len(m.group(1)))
    return None


_TOC_LINE = re.compile(r"…|\.{4,}|\s\d+\s*$")


def _alias_hit(line: str, aliases: List[str]) -> Optional[str]:
    """行命中词表别名 (守卫: 行≈别名)。
    守卫1 覆盖率: 去#编号装饰后的 bare ≥ 别名 60%;
    守卫2 标记行: 非标记行仅在纯别名行(bare≤别名+3)时放行 — 排除叙述句
    (实证澜起: '报告期末公司前三年主要会计数据和财务指标如下…' 曾冒充标题)。"""
    for al in aliases:
        if al not in line:
            continue
        if _TOC_LINE.search(line):
            continue                     # 目录条目 (点引导符/尾随页码)
        bare = _BARE_STRIP.sub("", line)
        al_bare = re.sub(r"[和及与]", "", al)
        if len(bare) < 0.6 * len(al_bare):
            continue
        if marker_rank(line) is None and len(bare) > len(al_bare) + 3:
            continue
        return al
    return None


def _block_end(md: str, from_pos: int, title_rank: float,
               max_chars: int = 12000) -> int:
    """内容块终点 = 下一个秩≤标题秩的标记行 (深层子标记是内容, 不截断)。"""
    for m in re.finditer(r"^[^\n]{0,80}$", md[from_pos:], re.M):
        line = m.group(0)
        rank = marker_rank(line)
        if rank is not None and rank <= title_rank:
            return from_pos + m.start()
        if m.start() > max_chars:
            return from_pos + m.start()
    return len(md)


# ---------------- ③ 分离与④ 清洗 ----------------

_ROW_RE = re.compile(r"^\|.+\|\s*$")


def _clean_cell(cell: str) -> str:
    """单元格清洗: <br> 是折行产物, 去除后跨行词自然接合。"""
    return cell.strip().strip("|").replace("<br>", "").strip()


def parse_md_tables(text: str) -> List[Dict]:
    """解析 MD 管道表。多分隔行拆多表; 内嵌口径行(如 '||2025年末|…')保留为数据行。"""
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
        current: Optional[Dict] = None
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
    """标题清洗: 去 #/装饰符(<u> ** 等)/编号, 留纯文本。
    装饰先剥 — 实证 '<u>(3)</u> …' 的编号在装饰符之内。"""
    t = re.sub(r"^#{1,6}\s*", "", line)
    t = re.sub(r"<[^>]+>|[*_]+", "", t)
    t = re.sub(r"^[（(]?(?:[一二三四五六七八九十]+|\d+)[）)]?[、．.]?\s*", "", t)
    t = re.sub(r"^\d+(?:\.\d+)*\s*", "", t)
    return t.strip()


def _header_lines(md: str) -> set:
    """全文级页眉集合: 出现≥10次的短名状行 (每页重复的公司名/年报名/栏目名)。"""
    from collections import Counter
    cnt = Counter(l.strip() for l in md.splitlines()
                  if l.strip() and len(l.strip()) <= 30)
    return {l for l, n in cnt.items()
            if n >= 10 and not l[-1] in "。；，：！？" and not l.startswith("|")}


def _clean_text(block: str, headers: set = frozenset()) -> str:
    """叙述文本: 剥表格行/页码行/标题标记行/重复页眉/HTML装饰/多余空行。

    重复页眉: 同一行在块内出现≥3次视为页眉 (每页重复的公司名/年报名, 实证澜起)。
    """
    from collections import Counter
    lines_ = []
    for line in block.splitlines():
        line = re.sub(r"<[^>]+>|[*_]+", "", line.rstrip())   # 装饰先剥
        if _ROW_RE.match(line) or _PAGE_NO.match(line.strip()):
            continue
        if marker_rank(line) is not None:      # 标题标记行是结构不是内容
            continue
        if line.strip() and line.strip() not in headers:
            lines_.append(line.strip())
    # 重复页眉: 短名状行(≤30字无句读)出现≥2次即页眉 (实证: '澜起科技股份有限公司')
    freq = Counter(lines_)
    return "\n".join(
        l for l in lines_
        if freq[l] < 2 or len(l) > 30 or l[-1] in "。；，：！？")


def _clean_tables(tables: List[Dict], title: str) -> List[Dict]:
    """表格清洗: 首行若为'单位：元'类单位行 → 并入表名 unit 字段并去除该行。"""
    for t in tables:
        h = t.get("header") or []
        if any("单位" in c for c in h) and sum(1 for c in h if c.strip()) <= 2:
            m = re.search(r"单位[:：]\s*([^\s|]+)", "".join(h))
            t["unit"] = m.group(1) if m else ""
            t["header"] = []
        t["name"] = title if not t.get("header") else title
    return tables


# ---------------- 编排 ----------------

def extract_items(md: str) -> Dict[str, Dict]:
    """全文 MD → {item_key: {title, text, tables}}。miss 容忍 (不出现的键不返回)。"""
    out: Dict[str, Dict] = {}
    headers = _header_lines(md)
    for item, aliases in ITEM_ALIASES.items():
        for m in re.finditer(r"^[^\n]{0,60}$", md, re.M):
            line = m.group(0)
            if _alias_hit(line, aliases) is None:
                continue
            rank = marker_rank(line)
            rank = rank if rank is not None else 2.0
            end = _block_end(md, m.end(), rank)
            block = md[m.end():end]
            # 短块守卫: ≥150字或含表格 (实证兴业 '2.3 非经常性损益' 合法地仅 243 字)
            if len(block.strip()) < 150 and "|" not in block:
                continue                     # 目录条目/页眉噪声, 试下一处
            title = _clean_title(line)
            tables = _clean_tables(parse_md_tables(block), title)
            um = re.search(r"^\s*单位[:：]\s*([^\s|]+)", block, re.M)
            if um and tables and not tables[0].get("unit"):
                tables[0]["unit"] = um.group(1)   # 表前独立单位行
            out[item] = {"title": title,
                         "text": _clean_text(block, headers),
                         "tables": tables}
            break
    return out


def build_items(stock: str, year: Optional[int] = None) -> Dict:
    """单股: 定位报告 PDF (年报→半年报降级链) → MD → 披露项 → JSON 落盘。"""
    from skills.content.annual_report import _locate_report_pdfs
    from skills.fin_query.skill import lookup_ts_code

    ts_code = lookup_ts_code(stock.strip())
    if not ts_code:
        return {"ok": False, "error": f"未识别股票: {stock}"}

    cands = _locate_report_pdfs(ts_code, year)
    if not cands:
        return {"ok": False, "error":
                f"{ts_code} 无已下载年报/半年报 PDF (先跑 tools.cninfo.downloader)"}

    from tools.pdf import to_markdown
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

    items = extract_items(md)
    json_path = out_dir / f"{loc['report_year']}_{loc['category']}_items.json"
    json_path.write_text(
        json.dumps({"ts_code": ts_code, "report_year": loc["report_year"],
                    "category": loc["category"], "items": items},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"披露项提取: {ts_code} {loc['report_year']} "
                f"{len(items)} 项 → {json_path}")
    return {"ok": True, "ts_code": ts_code, "report_year": loc["report_year"],
            "items": list(items), "json": str(json_path)}


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="年报披露项提取 (JSON 文件产物)")
    p.add_argument("stock", help="股票名或代码")
    p.add_argument("--year", type=int, default=0, help="指定报告年度")
    args = p.parse_args()
    r = build_items(args.stock, year=args.year or None)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
