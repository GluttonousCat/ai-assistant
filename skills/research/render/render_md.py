# -*- encoding: utf-8 -*-
"""
Stage 3 呈现层 — draft + figures → 可发布研究报告 MD (T08)

呈现层铁律: 只做排版与脚注化, 绝不改内容层正文 (禁"顺手润色")。
  - 行内 [E:eid]/[V:vid] → <sup>[n]</sup>, 编号按全文首次出现序, 同一引用复用同号;
  - 幽灵引用 (不在证据集) 原样保留为可见文本, 不伪装成脚注 (透明化, 配合 flags 区);
  - 文末"证据与观点来源"区与正文脚注编号一一对应, 每条可回溯;
  - flags 非空 → 文首"数据核查提示"块 (不隐藏问题);
  - 附录: 假设表(判断层) + 方法论说明(三层分离) + 免责声明。
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from core.logger import get_logger

from skills.research.contracts import Draft, Fact, View, pack_stats

logger = get_logger(__name__)

# 与 contracts._CITE_RE 同式: 行内引用 [E:eid] / [V:vid]
_CITE_RE = re.compile(r"\[(E|V):([^\]\s]+)\]")
_FIG_NAME_RE = re.compile(r"^ch(\d+)_")

_KIND_LABEL = {"tushare": "Tushare", "annual": "年报", "reports": "研报库",
               "other": "其他"}
_VIEW_CATEGORY_LABEL = {"viewpoint": "观点", "forecast": "预测",
                        "rating": "评级", "risk": "风险"}
_HYP_STATUS_LABEL = {"open": "待验证", "supported": "获支持",
                     "refuted": "被证伪", "needs_review": "需复核"}
_CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
           6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}

_METHODOLOGY = (
    "本报告采用**三层分离**方法组织内容: **事实层** (fact) 为可回溯到数据源的客观陈述"
    "（Tushare 字段、年报章节原文、公告），正文中以脚注显式关联证据编号; "
    "**观点层** (view) 为第三方研报主张，一律标注机构与日期，仅作观点转述，永不当作事实引用; "
    "**判断层** (judgment) 为本系统提出的研究假设，均附验证指标与当前状态，待后续数据检验闭环。"
    "文中数字结论均可按文末来源区逐条回溯核查；未经证据支持的数字一律标注\"待验证\"。"
)

_DISCLAIMER = (
    "本报告由个人研究系统自动生成，仅供学习与研究参考，不构成任何投资建议或买卖依据。"
    "数据以官方披露原文为准，研报观点归属原机构；自动化提取与换算可能存在误差，"
    "重要结论请按脚注回溯至原始来源核验。投资有风险，决策需独立。"
)


# ---------- 内部工具 ----------

def _cn_no(no: int) -> str:
    return _CN_NUM.get(no, str(no))


def _md_cell(s: str) -> str:
    """表格单元格转义: 竖线与换行会破坏表格布局"""
    return (s or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _rel_fig(fig_path: Path, base: Path) -> str:
    """图片路径相对化到报告所在目录 (MD 引用用相对路径, 统一正斜杠)"""
    try:
        if fig_path.is_absolute():
            return Path(os.path.relpath(fig_path, base)).as_posix()
    except ValueError:                 # Windows 跨盘符无相对路径
        return fig_path.as_posix()
    return Path(fig_path).as_posix()


def _norm_figs(figs: Sequence) -> List[Tuple[Path, Optional[int], Optional[str]]]:
    """T07 返回清单宽容归一: (path, ch_no[, hint]) / (path, ch_no) / 裸 Path。
    裸 Path 按文件名 chNN_ 约定绑章, 仍无法判定则挂文末附图区。"""
    out: List[Tuple[Path, Optional[int], Optional[str]]] = []
    for item in figs or []:
        if isinstance(item, (str, os.PathLike)):
            p, no, hint = Path(item), None, None
        else:
            seq = list(item)
            p = Path(seq[0])
            no = seq[1] if len(seq) > 1 else None
            hint = str(seq[2]) if len(seq) > 2 and seq[2] else None
        if no is None:
            m = _FIG_NAME_RE.match(p.name)
            no = int(m.group(1)) if m else None
        elif not isinstance(no, int):
            no = int(no)
        out.append((p, no, hint))
    return out


# ---------- 各区块渲染 ----------

def _title_block(draft: Draft, facts: List[Fact], views: List[View]) -> List[str]:
    stats = pack_stats(facts, views)
    kinds = sorted(stats["facts_by_kind"])
    kind_txt = "、".join(_KIND_LABEL.get(k, k) for k in kinds) if kinds else "无"
    n_orgs = len({v.org for v in views if v.org})
    lines = [
        f"# {draft.name}（{draft.ts_code}）研究报告",
        "",
        f"- 生成日期: {date.today().isoformat()}",
        (f"- 证据统计: {len(facts)} 条事实, 来自 {len(kinds)} 类来源（{kind_txt}）; "
         f"{len(views)} 条观点, 来自 {n_orgs} 家机构"),
        "",
    ]
    if draft.generated_at:
        lines.insert(3, f"- 草稿生成: {draft.generated_at}")
    return lines


def _flags_block(flags: List[str]) -> List[str]:
    if not flags:
        return []
    lines = [f"> **⚠ 数据核查提示** — 以下 {len(flags)} 条由引用校验器标记, "
             "请结合提示阅读正文（透明呈现, 不隐藏问题）:", ">"]
    lines += [f"> - {f}" for f in flags]
    lines += ["", "---", ""]
    return lines


def _assign_footnotes(draft: Draft, facts: List[Fact],
                      views: List[View]) -> Tuple[Dict[str, int], List[str], List[str]]:
    """按全文首次出现序为引用编号。返回 (ref→编号, 有序 ref 清单, 幽灵引用清单)。"""
    pool = {f.eid for f in facts} | {v.vid for v in views}
    order: List[str] = []
    ghosts: List[str] = []
    for ch in sorted(draft.chapters, key=lambda c: c.no):
        for m in _CITE_RE.finditer(ch.body_md):
            ref = m.group(2)
            if ref not in pool:
                if ref not in ghosts:
                    ghosts.append(ref)
                continue
            if ref not in order:
                order.append(ref)
    return {ref: i + 1 for i, ref in enumerate(order)}, order, ghosts


def _render_body(body_md: str, num: Dict[str, int]) -> str:
    def _sub(m: re.Match) -> str:
        n = num.get(m.group(2))
        return f"<sup>[{n}]</sup>" if n else m.group(0)   # 幽灵引用原样保留
    return _CITE_RE.sub(_sub, body_md)


def _source_lines(order: List[str], facts: List[Fact],
                  views: List[View]) -> List[str]:
    fmap = {f.eid: f for f in facts}
    vmap = {v.vid: v for v in views}
    lines: List[str] = []
    for i, ref in enumerate(order):
        n = i + 1
        if ref in fmap:
            f = fmap[ref]
            seg = [f"- **[{n}]** 事实 · {f.content}", f"来源: {f.source}"]
            if f.period:
                seg.append(f"期间: {f.period}")
            if f.confidence < 1.0:
                seg.append(f"注: 换算/推算值 (confidence={f.confidence})")
            lines.append(" — ".join(seg))
        else:
            v = vmap[ref]
            cat = _VIEW_CATEGORY_LABEL.get(v.category, v.category)
            head = f"- **[{n}]** 观点" + (f"（{cat}）" if cat else "")
            rid = f"研报 #{v.report_id}" if v.report_id else v.vid
            lines.append(f"{head} · {v.org}（{v.publish_date}）: {v.statement} — {rid}")
    return lines


def _fig_lines(figs: List[Tuple[Path, Optional[int], Optional[str]]],
               base: Path) -> List[str]:
    lines: List[str] = []
    for p, _no, hint in figs:
        if not p.is_file():
            logger.warning("配图不存在, 仍按原路径引用: %s", p)
        lines.append(f"![]({_rel_fig(p, base)})")
        if hint:
            lines += [f"*{hint}*", ""]
    return lines


def _hypotheses_table(draft: Draft) -> List[str]:
    lines = ["## 附录 A · 研究假设（判断层）", ""]
    if not draft.hypotheses:
        return lines + ["*（本篇未提出研究假设）*", ""]
    lines += ["| 编号 | 假设陈述 | 验证指标 | 状态 | 关联证据 |",
              "|---|---|---|---|---|"]
    for h in draft.hypotheses:
        status = _HYP_STATUS_LABEL.get(h.status, h.status)
        lines.append(
            f"| {_md_cell(h.hid)} | {_md_cell(h.statement)} | "
            f"{_md_cell('；'.join(x for x in h.indicators if x and x.strip()))} | "
            f"{status} | {_md_cell('；'.join(h.related_eids))} |")
    lines.append("")
    return lines


# ---------- 主入口 ----------

def render_report(draft: Draft, facts: List[Fact], views: List[View],
                  figs: Sequence, out_path: Path) -> Path:
    """draft + 证据包 + 配图清单 → 研究报告 MD, 返回落盘路径。

    figs 宽容接收 T07 清单 ((path, ch_no[, hint]) 元组或裸 Path, 裸 Path 按
    chNN_ 文件名约定绑章); out_path 为目标 md 路径, 图片引用相对其所在目录。
    """
    out_path = Path(out_path)
    base = out_path.parent
    fig_items = _norm_figs(figs)
    by_ch: Dict[int, List] = {}
    loose: List = []
    for p, no, hint in fig_items:
        (by_ch.setdefault(no, []) if no is not None else loose).append((p, no, hint))

    num, order, ghosts = _assign_footnotes(draft, facts, views)
    if ghosts:
        logger.warning("渲染发现 %d 条幽灵引用 (不在证据集, 原样保留): %s",
                       len(ghosts), ", ".join(ghosts))

    md: List[str] = []
    md += _title_block(draft, facts, views)
    md += _flags_block(draft.flags)
    for ch in sorted(draft.chapters, key=lambda c: c.no):
        md += [f"## {_cn_no(ch.no)}、{ch.title}", "", _render_body(ch.body_md, num), ""]
        if ch.no in by_ch:
            md += _fig_lines(by_ch[ch.no], base)
            md += [""]
    if loose:
        md += ["## 附图", ""]
        md += _fig_lines(loose, base)
        md += [""]
    md += ["## 证据与观点来源", ""]
    md += _source_lines(order, facts, views) or ["*（正文无引用）*"]
    md += [""]
    md += _hypotheses_table(draft)
    md += ["## 附录 B · 方法论说明", "", _METHODOLOGY, ""]
    md += ["## 免责声明", "", _DISCLAIMER, ""]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    logger.info("研究报告已渲染: %s (章节 %d, 脚注 %d, 配图 %d)",
                out_path, len(draft.chapters), len(order), len(fig_items))
    return out_path
