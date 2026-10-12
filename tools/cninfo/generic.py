# -*- encoding: utf-8 -*-
"""
通用年报处理逻辑 (L0) — 单一方言, 解决大多数标准格式年报, 零 LLM。

分工 (用户定调 2026-10-12):
- 本模块 = 低成本智能化: 只认 A 股年报披露准则的通用结构 —
  「第X节/章」章节锚 + 一、/(一)/N.M 三形态小节锚 + 第二章别名词表;
- 特例结构 (A+H 管治守则编号 / 主题式标题 / 混合式 / 目录陷阱…) 一律不追:
  契约机检 (contracts) 标记违约后交修复 Agent (repair) 语义兜底 = 最智能化;
- 机械层 (PDF→MD / 表格解析 / 清洗 / 装配) 复用 parse.py, 不在本模块重复。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .parse import (ITEM_ALIASES, SECTION_RULES, _alias_hit, _block_end,
                    _clean_title, _header_lines, _render_body, _sim,
                    _subsection_anchors, _subsection_dict, marker_rank)

# ---------------- 章节层: 唯一方言「第X节/章」 ----------------
# 排版容忍: # 前缀 (标准) / 纯粗体 '**第X章 …**' (招行实证, pymupdf 未标 #) —
# 结构锚仍是准则式第X节/章, 非结构特例; 主题式/混合式/A+H 编号归 Agent。
_SECTION_ANCHOR_RE = re.compile(
    r"^[#* \t]*第[一二三四五六七八九十百]+[节章][ \t]*(?P<title>[^\n]*?)[ \t*]*$",
    re.M)

# 目录页残留的节名行 1~3 个是常态 (旧版 ≥4 阈值实证), 少于此视为无准则式锚
_MIN_ANCHORS = 4

# 节名 → 内部键 (需求侧词表, 与 contracts 语义对应; 按名匹配不按序号)
_SECTION_KEYS = [
    ("管理层讨论", "mdna"), ("经营业绩", "mdna"), ("业务回顾", "mdna"),
    ("董事会报告", "mdna"), ("经营情况", "mdna"), ("业绩概述", "mdna"),
    ("业务概要", "mdna"), ("未来展望", "mdna"), ("专项分析", "mdna"),
    ("经营分析", "mdna"),
    ("公司治理", "governance"), ("ESG", "governance"),
    ("重要事项", "matters"), ("关联交易", "matters"),
    ("股份变动", "shareholders"), ("股东", "shareholders"),
    ("主要财务数据", "summary"), ("主要会计数据", "summary"),
    ("公司简介", "summary"),
    ("财务报表", "financial"), ("财务报告", "financial"),
    ("释义", "definitions"),
    ("债券", "bonds"),
]


def _section_key(title: str) -> str:
    for kw, key in _SECTION_KEYS:
        if kw in title:
            return key
    return "other"


def _clean_sec_title(title: str) -> str:
    """节名清洗: 剥勾选框/**/反引号装饰。"""
    from .parse import _CHECKBOX
    return re.sub(r"[*`]+", "", _CHECKBOX.sub("", title)).strip()


def split_sections(md: str) -> List[Tuple[str, str, str]]:
    """切节 (两个通用方言) → [(内部键, 节名, 节内文本)] 按文档序。

    ① 准则式: 「第X节/章」锚 ≥4 (目录页残留 1~3 属常态, 不足判无);
    ② 金融主题式: 无准则锚时, 取「可归一节名的标题 ≥3」的最浅标题级
       (银行/券商/保险年报格式类, 与准则式并列的通用结构, 非公司特例)。
    两者皆不命中返回空 — 契约违约派发 Agent。空标题节锚 (两行式) 取节内
    首个标题行。"""
    anchors = list(_SECTION_ANCHOR_RE.finditer(md))
    if len(anchors) >= _MIN_ANCHORS:
        out: List[Tuple[str, str, str]] = []
        for i, m in enumerate(anchors):
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(md)
            title = _clean_sec_title(m.group("title"))
            if not title:
                hm = re.search(r"^#{1,6}[ \t*`]*([^\n]+)$",
                               md[m.end():end][:400], re.M)
                if hm:
                    title = _clean_sec_title(hm.group(1))
            out.append((_section_key(title), title, md[m.end():end]))
        return out
    return _split_topic(md)


_TOPIC_HEAD_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>[^\n]+)$", re.M)


def _split_topic(md: str) -> List[Tuple[str, str, str]]:
    """金融主题式方言: 按可归一标题级切节 (该级全部标题定节界, 未映射节丢弃)。"""
    mapped: Dict[int, int] = {}
    for m in _TOPIC_HEAD_RE.finditer(md):
        title = m.group("title").strip()
        if title == "目录" or set(title) <= set("# *"):
            continue
        if _section_key(title) != "other":
            lvl = len(m.group("marks"))
            mapped[lvl] = mapped.get(lvl, 0) + 1
    lvl = next((l for l in sorted(mapped) if mapped[l] >= 3), None)
    if lvl is None:
        return []
    heads = [m for m in _TOPIC_HEAD_RE.finditer(md)
             if len(m.group("marks")) == lvl]
    out: List[Tuple[str, str, str]] = []
    for i, m in enumerate(heads):
        title = _clean_sec_title(m.group("title"))
        if not title or title == "目录":
            continue
        key = _section_key(title)
        if key == "other":
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        out.append((key, title, md[m.end():end]))
    return out


# ---------------- items: 第二章两项 (通用别名+相似度+守卫) ----------------

def extract_items(md: str, audit: Optional[Dict] = None) -> Dict[str, Dict]:
    """全文 MD → {item_key: {title, text, tables}}。miss 容忍 (缺失键交 Agent)。"""
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
            # 短块守卫: ≥150字或含表格 (目录条目/页眉噪声)
            if len(block.strip()) < 150 and "|" not in block:
                continue
            out[item] = _subsection_dict(_clean_title(line), block, headers)
            item_audit[item] = {
                "status": "exact" if exact else "fuzzy",
                "title": _clean_title(line),
                "score": 1.0 if exact else round(_sim(line, hit), 3)}
            break
        else:
            item_audit[item] = {"status": "miss"}
    if audit is not None:
        audit["items"] = item_audit
    return out


# ---------------- sections: 小节锚切分 (三形态) + 规则模式 ----------------

def extract_sections(md: str, audit: Optional[Dict] = None) -> Dict[str, Dict]:
    """切节 → 小节 → {section_key: {title, subsections}}。
    结构不符 (无小节锚 / keyword 零命中) 直接跳过记 audit — 派发 Agent。"""
    headers = _header_lines(md)
    out: Dict[str, Dict] = {}
    seen: Dict[str, str] = {}
    for key, sec_title, text in split_sections(md):
        rule = SECTION_RULES.get(key)
        if not rule or not text.strip():
            continue
        if rule.get("skip_short") and len(text.strip()) < rule["skip_short"]:
            seen[key] = "skip_short"
            continue
        anchors = _subsection_anchors(text)
        l1 = min((a[2] for a in anchors), default=None)
        entries: List[Tuple[str, str]] = []
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


def extract(md: str, audit: Optional[Dict] = None) -> Tuple[Dict, Dict]:
    """L0 编排: MD → (items, sections)。"""
    return extract_items(md, audit=audit), extract_sections(md, audit=audit)
