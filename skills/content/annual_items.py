# -*- encoding: utf-8 -*-
"""
年报披露项提取: 全文 MD → {items, sections} JSON (纯文件产物, 不碰 SQL)

  items     第二章精确两项 (词表+相似度兜底): kpi3y / nonrecurring
            (分季度数据 Tushare fin.income 季度期已覆盖, 走 SQL 不做提取)
  sections  三~七节按 L1 小节全量 (SECTION_RULES); 结构不符的节跳过不兜底,
            跳过原因进 audit (--audit) — 由面到点, 词表/规则按审计迭代

小节切分: 编号形态判兄弟 (一、=2.5 / N.M=3.0 / N.M.K=+0.1·段 / （一）=3.5),
# 层数不可信 — 澜起 L1 与 L2 同为 ####; 兴业 L1 混用 ####/#####/######。
方言/清洗/守卫的实证依据见 docs/agents/profile_extraction_handoff.md §5。

用法:
    python -m skills.content.annual_items 澜起科技 [--year 2025] [--audit]
    → output/cninfo/md/<代码>/<年度>_<类别>_items.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger

logger = get_logger(__name__)


def _locate_report_pdfs(ts_code: str,
                        year: Optional[int] = None) -> list:
    """年报+半年报候选 (报告期新→旧, 同期年报优先)。半年报与年报同构
    (管理层讨论与分析/风险章节), 年报为图片版无文本层时以半年报顶上。"""
    from storage.pg import PgClient
    try:
        with PgClient() as pg:
            return pg.fetch_all(
                """
                SELECT announcement_id, report_year, file_path, category
                FROM fin.cninfo_announcement
                WHERE ts_code=%s AND category=ANY('{ndbg,bndbg}')
                  AND download_status='done' AND file_path IS NOT NULL
                  AND (%s::int IS NULL OR report_year=%s)
                ORDER BY report_year DESC, category DESC
                """, (ts_code, year, year))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"报告定位查询失败: {e}")
        return []


MD_ROOT = Path("output/cninfo/md")

# 第二章披露项词表 (受控 item_key; 别名按实证扩充, 遇新措辞加一条)。
# 三~七节内容不再走词表 — sections 结构按小节全量, 覆盖面更完整。
ITEM_ALIASES: Dict[str, List[str]] = {
    "kpi3y": ["主要会计数据和财务指标", "主要财务数据及指标", "近三年主要会计数据",
              "近三年主要财务数据", "会计数据和财务指标摘要", "主要会计数据及财务指标",
              "财务概要", "财务摘要"],
    # 注: 不收「主要财务指标」短别名 — 会把 kpi3y 锚到第二节节标题
    # (澜起 '## 第二节 公司简介和主要财务指标' 实证); 该措辞由相似度兜底覆盖
    "nonrecurring": ["非经常性损益项目和金额", "非经常性损益项目及涉及金额",
                     "非经常性损益项目", "非经常性损益明细表", "非经常性损益表"],
}

# 勾选框后缀 '√适用□不适用' 常贴在标题尾 (万华600309/中铝601600 实证,
# 曾把非标记行守卫挤爆致 miss); 独立成行时是纯噪声
_CHECKBOX = re.compile(r"[□√]\s*(?:不\s*)?适\s*用(?:\s*[□√]\s*(?:不\s*)?适\s*用)?")

# ---------------- ① 定位与② 边界 ----------------

# 编号后容忍空格 (澜起 '#### 二、 经营情况讨论与分析' 实证 — 曾致 L1 误判深层)
_L1_NUM = re.compile(r"^[一二三四五六七八九十]+、\s*\S{0,50}$")
_L2_NUM = re.compile(r"^[（(][一二三四五六七八九十]+[）)]\s*\S{0,50}$")
_D_NUM = re.compile(r"^\d+(?:\.\d+)+\s*\S{0,50}$")
_HEAD = re.compile(r"^(#{1,6})\s+\S")
_PAGE_NO = re.compile(r"^\d+\s*/\s*\d+$")

_BARE_STRIP = re.compile(r"[#\s、。（(）)0-9.\-—第三节第四五六年度近<>*_/a-zA-Z]")


def marker_rank(line: str) -> Optional[float]:
    """行标记秩 (浅→深)。编号优先于#层级 — 实证澜起 L1(六、)与L2((一))同为####,
    #层数分不了兄弟, 必须按编号种类判。秩: #标题=层数; 一、=2.5; N.M=3.0;
    N.M.K=3.0+0.1·多段数 (兴业 '4.1.6'/'4.1.6.1' 与 '4.1' 同秩曾误切边界);
    （一）=3.5。** 粗体先剥 (兴业 '##### **4.1.6**' 实证)。"""
    line = _CHECKBOX.sub("", line.rstrip()).rstrip()   # 尾缀勾选框剥离
    body = re.sub(r"^#{1,6}\s*", "", line).replace("*", "")
    if _L1_NUM.match(body):
        return 2.5
    m = _D_NUM.match(body)
    if m:
        return 3.0 + 0.1 * (m.group(0).count(".") - 1)
    if _L2_NUM.match(body):
        return 3.5
    m = _HEAD.match(line)
    if m:
        return float(len(m.group(1)))
    return None


_CN_L1_LOOSE = re.compile(r"^[一二三四五六七八九十]+、\s*[^\n]{0,60}$")
_CN_L2_LOOSE = re.compile(r"^[（(][一二三四五六七八九十]+[）)]\s*[^\n]{0,60}$")
_NM_LOOSE = re.compile(r"^(\d+(?:\.\d+)+)\s*[^\n]{0,60}$")


def _anchor_rank(line: str) -> Optional[float]:
    """小节锚秩 (sections 路径): 只认编号形态, 裸 # 标题不算锚。

    # 标记行信任度高 — 标题放宽到 60 字可含空格/** (实证: 兴业
    '#### 5.5 …受处 罚情况' PDF 折行空格; 澜起 78 字长标题)。
    纯文本行走 marker_rank 严判 (叙述句防误切, 药明纯文本标题实证)。"""
    line = _CHECKBOX.sub("", line.rstrip()).rstrip()
    if _HEAD.match(line) is None:
        return marker_rank(line)
    body = re.sub(r"^#{1,6}\s*", "", line).replace("*", "")
    if _CN_L1_LOOSE.match(body):
        return 2.5
    if _CN_L2_LOOSE.match(body):
        return 3.5
    m = _NM_LOOSE.match(body)
    if m:
        return 3.0 + 0.1 * (m.group(1).count(".") - 1)
    return None


_TOC_LINE = re.compile(r"…|\.{4,}|\s\d+\s*$")

_SIM_STRIP = re.compile(r"[\s、，,。；;：:（）()\[\]【】/**#0-9.\-—·_`]")


def _sim(line: str, alias: str) -> float:
    """归一化字符相似度 (difflib 比率)。剥空白/标点/数字/装饰后比对 —
    '非经常性损益项目及涉及金额' vs '非经常性损益项目和金额' ≈ 0.87。"""
    a = _SIM_STRIP.sub("", line)
    b = _SIM_STRIP.sub("", alias)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _alias_hit(line: str, aliases: List[str]) -> Optional[str]:
    """行命中词表别名: 精确包含 → 相似度兜底 (标记行≥0.75/纯文本行≥0.85)。
    守卫: ①目录条目排除; ②覆盖率 bare ≥ 别名 60%; ③非标记行仅在纯别名行
    (bare≤别名+3, 兜底通道收紧到 ≤30) 放行 — 排除叙述句 (实证澜起:
    '报告期末公司前三年主要会计数据和财务指标如下…' 曾冒充标题)。"""
    line = _CHECKBOX.sub("", line).rstrip()
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
    # 相似度兜底: 仅结构守卫内的行参与; 非标记行 (纯文本, 信任低) 阈值收紧
    if _TOC_LINE.search(line):
        return None
    bare = _BARE_STRIP.sub("", line)
    is_marker = marker_rank(line) is not None
    if not is_marker and len(bare) > 30:
        return None
    best_al, best_r = None, 0.0
    for al in aliases:
        r = _sim(line, al)
        if r > best_r:
            best_al, best_r = al, r
    threshold = 0.75 if is_marker else 0.85
    return best_al if best_r >= threshold else None


def _block_end(md: str, from_pos: int, title_rank: float) -> int:
    """内容块终点 = 下一个秩≤标题秩的标记行 (深层子标记是内容, 不截断)。"""
    for m in re.finditer(r"^[^\n]{0,80}$", md[from_pos:], re.M):
        line = m.group(0)
        rank = marker_rank(line)
        if rank is not None and rank <= title_rank:
            return from_pos + m.start()
        if m.start() > 12000:
            return from_pos + m.start()
    return len(md)


# ---------------- 节层: 双方言定位 (§5.1) ----------------

# 兴业实证: '### **第一章 公司简介**' — 章式+粗体装饰, 旧版只认 '第X节' 故漏
_SECTION_ANCHOR_RE = re.compile(
    r"^#{1,6}[ \t*]*第[一二三四五六七八九十百]+[节章][ \t]*(?P<title>[^\n]*?)[ \t*]*$",
    re.M)

# 节名 → 内部键 (按名匹配不按序号; 实证: 各家合并节方式不一,
# 中石化 '关联交易' 独立成节而准则式并入重要事项)
_SECTION_KEYS = [
    ("管理层讨论", "mdna"), ("经营业绩", "mdna"), ("业务回顾", "mdna"),
    ("董事会报告", "mdna"), ("经营情况", "mdna"), ("业绩概述", "mdna"),
    ("业务概要", "mdna"), ("未来展望", "mdna"), ("专项分析", "mdna"),
    ("公司治理", "governance"), ("ESG", "governance"),
    ("重要事项", "matters"), ("关联交易", "matters"),
    ("股份变动", "shareholders"), ("股东", "shareholders"),
    ("主要财务数据", "summary"), ("主要会计数据", "summary"),
    ("公司简介", "summary"),
    ("财务报表", "financial"), ("财务报告", "financial"),
    ("释义", "definitions"),
    ("债券", "bonds"),
]

_TOPIC_HEAD_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>[^\n]+)$", re.M)


def _section_key(title: str) -> str:
    for kw, key in _SECTION_KEYS:
        if kw in title:
            return key
    return "other"


def _pick_topic_level(md: str) -> Optional[int]:
    """主题式方言的节标题级: 可归一节名数 ≥3 的最浅标题级。

    按可归一数而非标题总数 — 中石化 ### 级有 4 个杂项标题
    ('释义：'/'换算比例：'/走势图/'**1.**') 但仅释义可归一, 按总数会误选 ###。"""
    mapped: Dict[int, int] = {}
    for m in _TOPIC_HEAD_RE.finditer(md):
        title = m.group("title").strip()
        if title == "目录" or set(title) <= set("# *"):
            continue
        if _section_key(title) != "other":
            lvl = len(m.group("marks"))
            mapped[lvl] = mapped.get(lvl, 0) + 1
    for lvl in sorted(mapped):
        if mapped[lvl] >= 3:
            return lvl
    return None


def split_sections_titled(md: str) -> List[Tuple[str, str, str]]:
    """切节 (双方言)。返回 [(内部键, 节名, 节内文本)] 按文档序。

    「第X节/章」锚 ≥4 → 准则式 (目录页有 1~3 个残留, ≥1 就判会误入 —
    兴业实证踩坑); 否则主题式动态选节级 (中石化 ## 级实证)。

    空标题节锚 (华泰'## 第一节'+下一行'## 释义' 两行式, 中建'##### 第一节'
    +'### **释义**' 实证): 节名取节内首个标题行。节名统一剥 **/反引号
    装饰 (工行 '## **5.** `财务概要`' 实证)。"""
    anchors = list(_SECTION_ANCHOR_RE.finditer(md))
    if len(anchors) >= 4:
        out = []
        for i, m in enumerate(anchors):
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(md)
            title = _clean_sec_title(m.group("title"))
            if not title:
                hm = re.search(r"^#{1,6}[ \t*`]*([^\n]+)$",
                               md[m.end():end][:400], re.M)
                if hm:
                    title = _clean_sec_title(hm.group(1))
            out.append((_section_key(title), title, md[m.end():end]))
        # 混合式补齐 (中建601668实证: '第X节'锚仅首尾各二, 中段节是
        # '## **管理层讨论 与分析**' 主题标题): 未覆盖的键取首个可归一标题,
        # 节界按同级下一标题
        covered = {k for k, _t, _x in out}
        heads: Dict[int, list] = {}
        for hm in _TOPIC_HEAD_RE.finditer(md):
            heads.setdefault(len(hm.group("marks")), []).append(hm)
        for lvl in sorted(heads):
            for j, hm in enumerate(heads[lvl]):
                title = _clean_sec_title(hm.group("title"))
                key = _section_key(title) if title else "other"
                if key == "other" or key in covered:
                    continue
                hs = heads[lvl]
                end = hs[j + 1].start() if j + 1 < len(hs) else len(md)
                out.append((key, title, md[hm.end():end]))
                covered.add(key)
        return out
    lvl = _pick_topic_level(md)
    if lvl is None:
        return []
    heads = [m for m in _TOPIC_HEAD_RE.finditer(md)
             if len(m.group("marks")) == lvl]
    out = []
    for i, m in enumerate(heads):
        title = _clean_sec_title(m.group("title"))
        if not title or title == "目录":
            continue
        key = _section_key(title)
        if key == "other":             # 节界仍按该级全部标题切, 未映射节丢弃
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        out.append((key, title, md[m.end():end]))
    return out


def _clean_sec_title(title: str) -> str:
    """节名清洗: 剥勾选框/**/反引号装饰 (中建 '**债券相关情况' 曾带尾星)。"""
    return re.sub(r"[*`]+", "", _CHECKBOX.sub("", title)).strip()


# ---------------- 小节层: 编号锚切分 (§5.2) ----------------

_PUNCT_END = ("。", "；", "，", "：", ":", "！", "？")


def _subsection_anchors(text: str) -> List[Tuple[int, int, float]]:
    """节内编号锚扫描。返回 [(锚行起始, 锚行结束, 锚秩)] 按序。

    纯文本锚守卫 (药明实证: pymupdf4llm 未标记的 '十四、募集资金…'):
    ≤50 字且不以句读结尾 — 叙述段落(长行/带句号)不命中。
    # 标记锚守卫放宽到 120 字 (澜起 78 字长标题实证)。"""
    out = []
    for m in re.finditer(r"^[^\n]{1,120}$", text, re.M):
        line = m.group(0)
        rank = _anchor_rank(line)
        if rank is None:
            continue
        if _HEAD.match(line) is None:
            s = line.strip()
            if len(s) > 50 or s.endswith(_PUNCT_END):
                continue
        out.append((m.start(), m.end(), rank))
    return out


def _render_body(section_text: str,
                 anchors: List[Tuple[int, int, float]], i: int) -> str:
    """anchors[i] 的 L1 正文 = 直述 + 其后连续深层子块; 子块标题转 [标题] 行
    保留 (直接清洗会剥掉标记行标题 — 澜起 '五、主要经营情况' 的 (一)(二)
    子节标题实证)。终点 = 下一秩≤本秩锚 (裸 # 标题不算边界 — 兴业第一章
    '### 公司组织架构图' 与 '#### 1.1' 同视觉层级, 按 # 层切曾丢内容)。"""
    _, a_end, rank = anchors[i]
    end = len(section_text)
    for j in range(i + 1, len(anchors)):
        if anchors[j][2] <= rank:
            end = anchors[j][0]
            break
    parts, pos = [], a_end
    for j in range(i + 1, len(anchors)):
        s, e, _r = anchors[j]
        if s >= end:
            break
        parts.append(section_text[pos:s])
        parts.append(f"\n[{_clean_title(section_text[s:e])}]\n")
        pos = e
    parts.append(section_text[pos:end])
    return "".join(parts)


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
    """标题清洗: 去 #/装饰符(<u> ** 等)/编号/勾选框, 留纯文本。
    装饰先剥 — 实证 '<u>(3)</u> …' 的编号在装饰符之内。"""
    t = _CHECKBOX.sub("", line)
    t = re.sub(r"^#{1,6}\s*", "", t)
    t = re.sub(r"<[^>]+>|[*_`]+", "", t)
    t = re.sub(r"^[（(]?(?:[一二三四五六七八九十]+|\d+)[）)]?[、．.]?\s*", "", t)
    t = re.sub(r"^\d+(?:\.\d+)*\s*", "", t)
    return t.strip()


def _header_lines(md: str) -> set:
    """全文级页眉集合: 出现≥10次的短名状行 (每页重复的公司名/年报名/栏目名)。"""
    cnt = Counter(l.strip() for l in md.splitlines()
                  if l.strip() and len(l.strip()) <= 30)
    return {l for l, n in cnt.items()
            if n >= 10 and not l[-1] in "。；，：！？" and not l.startswith("|")}


_BARE_PAGE = re.compile(r"^\d{1,4}$")


def _clean_text(block: str, headers: set = frozenset()) -> str:
    """叙述文本: 剥表格行/页码行('N / M' 与兴业裸数字'15')/标题标记行/
    勾选框/重复页眉/HTML装饰。页眉双法: 全文集(≥10次短行) + 块内频次
    (≥2次, 实证 '澜起科技股份有限公司')。"""
    lines_ = []
    for line in block.splitlines():
        line = re.sub(r"<[^>]+>|[*_]+", "", line.rstrip())   # 装饰先剥
        line = _CHECKBOX.sub("", line)              # 勾选框行/尾缀 (纯行变空被滤)
        s = line.strip()
        if _ROW_RE.match(line) or _PAGE_NO.match(s) or _BARE_PAGE.match(s):
            continue
        if marker_rank(line) is not None:      # 标题标记行是结构不是内容
            continue
        if s and s not in headers:
            lines_.append(s)
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
        t["name"] = title
    return tables


def _subsection_dict(title: str, body: str, headers: set) -> Dict:
    """小节三件套 {title, text, tables}: 复用 items 的清洗管线。"""
    tables = _clean_tables(parse_md_tables(body), title)
    um = re.search(r"^\s*单位[:：]\s*([^\s|]+)", body, re.M)
    if um and tables and not tables[0].get("unit"):
        tables[0]["unit"] = um.group(1)   # 表前独立单位行
    return {"title": title, "text": _clean_text(body, headers),
            "tables": tables}


# ---------------- 三~七节规则 (§3, 用户拍板) ----------------

SECTION_RULES: Dict[str, Dict] = {
    "mdna":         {"mode": "all"},          # 全量 (用户原话"全部重要")
    "governance":   {"mode": "keyword",
                     "keywords": ["董监高", "董事和高级管理人员", "员工",
                                  "股权激励", "审计委员会"]},
    "matters":      {"mode": "keyword",
                     "keywords": ["募集资金", "募投", "承诺", "关联交易",
                                  "担保", "诉讼", "退市", "非标"]},
    "shareholders": {"mode": "all", "include_head": True},   # 节首前文=股东总数字段
    "bonds":        {"mode": "all", "skip_short": 500},      # 多数公司"不适用"
}


def extract_sections(md: str, audit: Optional[Dict] = None) -> Dict[str, Dict]:
    """全文 MD → {section_key: {title, subsections: [{title, text, tables}]}}。

    同键多节合并 (中石化 mdna = 业绩回顾/管理层讨论/董事会报告 三主题节拼接)。
    结构不符的节直接跳过不兜底 (用户决策「不和规范的先跳过」): 节内无编号
    小节 / keyword 零命中 — 原因写入 audit。L1 = 节内最浅锚秩 (澜起 '一、'
    =2.5 / 兴业 'N.M'=3.0), 深层子锚由 _render_body 并入父块。"""
    headers = _header_lines(md)
    out: Dict[str, Dict] = {}
    seen: Dict[str, str] = {}
    for key, sec_title, text in split_sections_titled(md):
        rule = SECTION_RULES.get(key)
        if not rule or not text.strip():
            continue
        if rule.get("skip_short") and len(text.strip()) < rule["skip_short"]:
            seen[key] = "skip_short"
            continue
        anchors = _subsection_anchors(text)
        l1 = min((a[2] for a in anchors), default=None)
        entries: List[Tuple[str, str]] = []    # [(标题, 正文)]
        if l1 is not None and rule.get("include_head"):
            head = text[:next(a[0] for a in anchors if a[2] == l1)]
            if head.strip():
                entries.append(("(节首前文)", head))
        for i, (a_start, a_end, a_rank) in enumerate(anchors):
            if a_rank != l1:
                continue
            title = _clean_title(text[a_start:a_end])
            if rule["mode"] == "keyword" and not any(
                    kw in title for kw in rule["keywords"]):
                continue
            entries.append((title, _render_body(text, anchors, i)))
        if not entries:
            seen[key] = "skip_no_keyword" if l1 is not None else "skip_no_anchors"
            continue
        sec = out.setdefault(key, {"title": _clean_title(sec_title),
                                   "subsections": []})
        for title, body in entries:
            sec["subsections"].append(_subsection_dict(title, body, headers))
        seen[key] = "ok"
    if audit is not None:
        audit["sections"] = {
            key: ({"status": "ok", "n": len(out[key]["subsections"])}
                  if key in out else {"status": seen.get(key, "miss"), "n": 0})
            for key in SECTION_RULES}
    return out


# ---------------- 编排 ----------------

def extract_items(md: str, audit: Optional[Dict] = None) -> Dict[str, Dict]:
    """全文 MD → {item_key: {title, text, tables}}。miss 容忍 (不出现的键不返回)。

    audit 形如 {"items": {key: {"status": "exact|fuzzy|miss", "title": …,
    "score": 相似度}}} — 批量跑完审一遍, fuzzy/miss 的措辞补进词表。"""
    out: Dict[str, Dict] = {}
    headers = _header_lines(md)
    item_audit: Dict[str, Dict] = {}
    for item, aliases in ITEM_ALIASES.items():
        for m in re.finditer(r"^[^\n]{0,60}$", md, re.M):
            line = m.group(0)
            hit = _alias_hit(line, aliases)
            if hit is None:
                continue
            exact = hit in line
            rank = marker_rank(line)
            rank = rank if rank is not None else 2.0
            end = _block_end(md, m.end(), rank)
            block = md[m.end():end]
            # 短块守卫: ≥150字或含表格 (实证兴业 '2.3 非经常性损益' 合法地仅 243 字)
            if len(block.strip()) < 150 and "|" not in block:
                continue                     # 目录条目/页眉噪声, 试下一处
            title = _clean_title(line)
            out[item] = _subsection_dict(title, block, headers)
            item_audit[item] = {
                "status": "exact" if exact else "fuzzy",
                "title": title, "score": 1.0 if exact else round(_sim(line, hit), 3)}
            break
        else:
            item_audit[item] = {"status": "miss"}
    if audit is not None:
        audit["items"] = item_audit
    return out


def build_items(stock: str, year: Optional[int] = None,
                want_audit: bool = False) -> Dict:
    """单股: 定位报告 PDF (年报→半年报降级链) → MD → items+sections → JSON 落盘。"""
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

    audit: Dict = {}
    items = extract_items(md, audit=audit if want_audit else None)
    sections = extract_sections(md, audit=audit if want_audit else None)
    json_path = out_dir / f"{loc['report_year']}_{loc['category']}_items.json"
    json_path.write_text(
        json.dumps({"ts_code": ts_code, "report_year": loc["report_year"],
                    "category": loc["category"], "items": items,
                    "sections": sections},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    sec_stat = {k: len(v["subsections"]) for k, v in sections.items()}
    logger.info(f"披露项提取: {ts_code} {loc['report_year']} "
                f"items {list(items)} + sections {sec_stat} → {json_path}")
    r = {"ok": True, "ts_code": ts_code, "report_year": loc["report_year"],
         "items": list(items), "sections": sec_stat, "json": str(json_path)}
    if want_audit:
        r["audit"] = audit
    return r


def build_for_index(index_code: str, year: Optional[int] = None) -> Dict[str, int]:
    """指数成分股批量 → 每股 items.json (Tushare 取最新成分, 逐股重提)。"""
    import time
    from datetime import date, timedelta
    import tushare as ts
    from core.config import get_config

    pro = ts.pro_api(get_config().tushare_token)
    start = (date.today() - timedelta(days=95)).strftime("%Y%m%d")
    df = pro.index_weight(
        index_code=index_code,
        start_date=start, end_date=date.today().strftime("%Y%m%d"))
    latest = df["trade_date"].max()
    codes = sorted(df[df["trade_date"] == latest]["con_code"].unique())
    logger.info(f"披露项批量: {index_code} {len(codes)} 只")
    stats = {"ok": 0, "err": 0}
    for i, code in enumerate(codes, 1):
        r = build_items(code, year=year)
        stats["ok" if r["ok"] else "err"] += 1
        if not r["ok"]:
            logger.warning(f"[{i}/{len(codes)}] {code} 失败: {r['error']}")
        else:
            logger.info(f"[{i}/{len(codes)}] {code} {len(r['items'])}项 "
                        f"{r.get('sections', {})}")
        time.sleep(1)
    return stats


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="年报披露项提取 (JSON 文件产物)")
    p.add_argument("stock", nargs="?", default="", help="股票名或代码")
    p.add_argument("--year", type=int, default=0, help="指定报告年度")
    p.add_argument("--index", default="", help="指数成分批量 (如 000016.SH)")
    p.add_argument("--audit", action="store_true",
                   help="输出审计 (items 命中方式/sections 跳过原因)")
    args = p.parse_args()
    if args.index:
        stats = build_for_index(args.index, year=args.year or None)
        print(f"[ITEMS] {stats}")
        return 0 if stats["err"] == 0 else 1
    if not args.stock:
        p.error("stock 或 --index 至少一个")
    r = build_items(args.stock, year=args.year or None, want_audit=args.audit)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
