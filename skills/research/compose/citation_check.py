# -*- encoding: utf-8 -*-
"""引用校验器 (T06) — 纯逻辑, 不连 LLM/DB, 全部用例可离线跑。

对草稿 (Draft) 做四类检查, 结果以 flag 字符串列表返回, 由 generator 写入
Draft.flags (供 T09 评分卡与 T08 渲染警告区消费):

  unverified-number    数字句启发: 句子 (剔除引用标注与纯年份/期间表达后) 仍含数字,
                       且整句无 [E:]/[V:] 标注 → 待人工核对 (显式写「待验证」的句子豁免)
  ghost-citation       幽灵引用: 正文行内引用 / used_eids / 假设 related_eids
                       指向证据集中不存在的 eid/vid → 硬伤
  used-eids-mismatch   used_eids 与正文行内引用不一致 (单向漏记) → 硬伤
  view-style-hint      宽松提示 (不强制): 含 [V:] 的句子宜有机构名或「认为/预计」等字样

flag 一律「类别: 定位 描述」格式, 前缀即类别, 消费方可按前缀分级。
同类告警每章超过 MAX_FLAGS_PER_CHAPTER 条时聚合为一条, 防止 flags 爆炸。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set, Tuple

from skills.research.contracts import Draft, Fact, View

# 行内引用 [E:eid] / [V:vid] (与 contracts._CITE_RE 同口径; 此处公开导出供生成器/测试复用)
CITE_RE = re.compile(r"\[(E|V):([^\]\s]+)\]")

# 纯年份/期间表达: 从句中剔除后无数字残留, 视为「仅提及时间」而非数据断言。
# 覆盖: 2025 / 2025年 / 2021-2025 / 2021年-2025年 / 2025A / 2025Q3 / 2025H1 / Q3 / H1 / 3季度
_PERIOD_ONLY_RE = re.compile(
    r"(?:19|20)\d{2}\s*(?:[-—~至]\s*(?:19|20)\d{2})?年?(?:A|Q[1-4]|H1)?"
    r"|[一二三四1-4]\s*季度|(?<![A-Za-z0-9])(?:Q[1-4]|H1)(?![A-Za-z0-9])"
)
_HAS_DIGIT = re.compile(r"\d")

# 句边界: 句号/叹号/问号/换行 (分号逗号不切 — 引用按整句覆盖)
_SENT_SPLIT = re.compile(r"[。！？!?\n]")
# 非正文行前缀: 标题与表格行 (其中的数字不作"句子"启发)
_NON_PROSE_PREFIX = ("#", "|")
_SENT_STRIP = " \t;；,，、-"

# view 句宽松判词 (任务书: 机构名或"认为"字样; 取近义判词放宽, 避免误报)
_VIEW_SOFT_WORDS = ("认为", "预计", "预期", "预测", "判断", "展望")

MAX_FLAGS_PER_CHAPTER = 8    # 同类告警每章上限
_DIGEST_LEN = 50             # 句摘要保留长度


def extract_citations(text: str) -> List[Tuple[str, str]]:
    """提取行内引用 → [("E", eid), ("V", vid), ...], 按出现序保留重复。"""
    return [(m.group(1), m.group(2)) for m in CITE_RE.finditer(text)]


def iter_sentences(body_md: str) -> Iterable[str]:
    """Markdown 正文 → 句子流: 跳过标题/表格行, 每行再按句末标点切分。"""
    for line in body_md.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_NON_PROSE_PREFIX):
            continue
        for sent in _SENT_SPLIT.split(line):
            sent = sent.strip(_SENT_STRIP)
            if sent:
                yield sent


def _digest(s: str) -> str:
    """句摘要: 去空白后截断。"""
    s = re.sub(r"\s+", "", s)
    return s[:_DIGEST_LEN] + ("…" if len(s) > _DIGEST_LEN else "")


def check_unverified_numbers(no: int, body_md: str) -> List[str]:
    """数字句启发 (第 no 章): 句子含数据断言 (剔除年份后仍有数字) 且句内无
    任何 [E:]/[V:] 标注 → flag (有标注的句子交给存在性/观点规范检查负责)。"""
    hits: List[str] = []
    for sent in iter_sentences(body_md):
        if CITE_RE.search(sent):
            continue                       # 句内已有引用标注
        if "待验证" in sent:
            continue                       # 规则允许: 无证据数字显式声明「待验证」
        bare = CITE_RE.sub("", sent)       # 引用编号本身含数字, 兜底剔除后再判定
        if _HAS_DIGIT.search(_PERIOD_ONLY_RE.sub("", bare)):
            hits.append(_digest(bare))
    flags = [f"unverified-number: 第{no}章「{h}」" for h in hits[:MAX_FLAGS_PER_CHAPTER]]
    if len(hits) > MAX_FLAGS_PER_CHAPTER:
        flags.append(f"unverified-number: 第{no}章 另有 {len(hits) - MAX_FLAGS_PER_CHAPTER} 句"
                     f"无引用数字 (已截断)")
    return flags


def check_ghost_citations(draft: Draft, eid_set: Set[str], vid_set: Set[str]) -> List[str]:
    """幽灵引用: 正文行内引用 / used_eids / 假设 related_eids 不在证据集 → flag。"""
    flags: List[str] = []
    for ch in draft.chapters:
        for kind, ref in extract_citations(ch.body_md):
            if ref not in (eid_set if kind == "E" else vid_set):
                flags.append(f"ghost-citation: 第{ch.no}章 行内引用 [{kind}:{ref}] 不在证据集")
        for ref in ch.used_eids:
            if ref not in (vid_set if ref.startswith("R:") else eid_set):
                flags.append(f"ghost-citation: 第{ch.no}章 used_eids 引用 {ref!r} 不在证据集")
    for h in draft.hypotheses:
        for ref in h.related_eids:
            if ref not in (vid_set if ref.startswith("R:") else eid_set):
                flags.append(f"ghost-citation: 假设 {h.hid} related_eids 引用 {ref!r} 不在证据集")
    return flags


def check_used_eids_consistency(draft: Draft) -> List[str]:
    """used_eids 与正文行内引用集合双向核对, 漏记/多记 → flag。"""
    flags: List[str] = []
    for ch in draft.chapters:
        inline = {ref for _, ref in extract_citations(ch.body_md)}
        registered = set(ch.used_eids)
        extra = sorted(registered - inline)
        missing = sorted(inline - registered)
        if extra:
            flags.append(f"used-eids-mismatch: 第{ch.no}章 used_eids 有 {len(extra)} 项正文未引用 "
                         f"({', '.join(extra[:3])}{'…' if len(extra) > 3 else ''})")
        if missing:
            flags.append(f"used-eids-mismatch: 第{ch.no}章 正文引用有 {len(missing)} 项未记入 "
                         f"used_eids ({', '.join(missing[:3])}{'…' if len(missing) > 3 else ''})")
    return flags


def check_view_style(no: int, body_md: str, views: List[View]) -> List[str]:
    """宽松提示: 含 [V:] 的句子宜出现所引观点的机构名或「认为/预计」等字样。"""
    vid2org: Dict[str, str] = {v.vid: v.org for v in views}
    hits: List[str] = []
    for sent in iter_sentences(body_md):
        vids = [ref for kind, ref in extract_citations(sent) if kind == "V"]
        if not vids:
            continue
        has_org = any(vid2org.get(v, "") and vid2org[v] in sent for v in vids)
        if not has_org and not any(w in sent for w in _VIEW_SOFT_WORDS):
            hits.append(f"[V:{vids[0]}]「{_digest(CITE_RE.sub('', sent))}」")
    flags = [f"view-style-hint: 第{no}章 {h}" for h in hits[:MAX_FLAGS_PER_CHAPTER]]
    if len(hits) > MAX_FLAGS_PER_CHAPTER:
        flags.append(f"view-style-hint: 第{no}章 另有 {len(hits) - MAX_FLAGS_PER_CHAPTER} 句"
                     f"无机构名/判词 (已截断)")
    return flags


def check_draft(draft: Draft, facts: List[Fact], views: List[View]) -> List[str]:
    """全稿校验入口: 返回按序去重后的 flags (生成器把它并入 Draft.flags)。"""
    eid_set = {f.eid for f in facts}
    vid_set = {v.vid for v in views}
    flags: List[str] = []
    for ch in draft.chapters:
        flags += check_unverified_numbers(ch.no, ch.body_md)
        flags += check_view_style(ch.no, ch.body_md, views)
    flags += check_ghost_citations(draft, eid_set, vid_set)
    flags += check_used_eids_consistency(draft)
    seen: Set[str] = set()
    return [f for f in flags if not (f in seen or seen.add(f))]
