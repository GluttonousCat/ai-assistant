# -*- encoding: utf-8 -*-
"""
T07 · 图表工场 (Stage 3) — 从证据时序自动产出章节配图

build_figures(draft, facts, research_dir) -> List[FigureOut(path, chapter_no, hint)]
不改 draft.json; 返回 (路径, 章号, 插入位置提示) 清单, 由 T08 组装时插入对应章末。

图类型路由 (铁律: 财报累计口径禁止折线连点, 同指标多年一律柱状):
  收入构成          → 占比条形   ch02 商业模式与业务拆解   (T05 wants: mainbiz)
  营收/净利多年     → 双轴分组柱 ch04 历史经营与财务质量   (wants: financials; 同比红涨绿跌)
  毛利率/净利率/ROE → 分组柱     ch04
  净利润与经营现金流→ 分组柱     ch04 (盈利质量: 现金流与利润匹配)
  产量/销量         → 分组柱     ch05 经营驱动因子与盈利机制 (wants: annual/financials)
  产能利用率        → 柱状       ch05
  研发投入          → 柱状       ch06 技术、研发与竞争优势 (wants: financials)
  估值水位          → 区间带     ch10 估值与持续跟踪       (wants: financials; 行情数据非累计口径)

风格对齐 skills/content/charts.py (深黑暗棕/微软雅黑/1080px, 只 import 不修改);
每张图只消费 fact 层证据 (三层分离), 图脚注带 eid 溯源; A股红涨绿跌 (同比标注)。
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import matplotlib

matplotlib.use("Agg")  # 纯批量出图模块, 先于任何 pyplot 导入固定后端 (离线可跑)

from core.logger import get_logger
from skills.content.charts import (_ACCENT, _ACCENT2, _DIM, _FG, _GRID, _NEG,
                                   _POS, _finish, _new_fig, _style_ax)
from skills.research.contracts import Draft, Fact

logger = get_logger(__name__)

FIG_WIDTH = 10.8  # 公众号宽度 1080px (dpi=100)
_FIGS_SUBDIR = "figs"


class FigureOut(NamedTuple):
    """T07→T08 交接物: 图片路径 + 归属章号 + 插入位置提示 (可按三元组解包)"""
    path: Path
    chapter_no: int
    hint: str


# ============================================================
# 证据时序整理: eid -> 指标序列
# ============================================================

# 分类规则: (类别, 指标键正则[小写匹配], 内容关键词) — 顺序即优先级, 特异在前
# (netmargin 先于 profit, 防 netprofit_margin 被 "netprofit" 误吞; pe/pb 用词边界防 capex)
_RULES: List[Tuple[str, List[str], List[str]]] = [
    ("netmargin", [r"netprofit_?margin", r"净利率"], ["净利率"]),
    ("gross", [r"grossprofit", r"毛利率"], ["毛利率"]),
    ("roe", [r"(?<![a-z0-9])roe(?![a-z0-9])"], ["ROE", "净资产收益率"]),
    ("pe", [r"(?<![a-z0-9])pe(?:_ttm)?(?![a-z0-9])", r"市盈率"], ["市盈率", "PE"]),
    ("pb", [r"(?<![a-z0-9])pb(?![a-z0-9])", r"市净率"], ["市净率", "PB"]),
    ("ocf", [r"netcash_?operate", r"(?<![a-z0-9])ocf(?![a-z0-9])", r"经营现金流"],
     ["经营现金流"]),
    ("util", [r"util", r"产能利用率"], ["产能利用率"]),
    ("volume", [r"production", r"output", r"产量"], ["产量"]),
    ("salesvol", [r"sales_?vol", r"销量"], ["销量"]),
    ("revenue", [r"revenue", r"营业收入", r"营收"], ["营业收入", "营收"]),
    ("profit", [r"n_?income", r"netprofit", r"归母净利", r"净利润"], ["归母净利", "净利润"]),
    ("rd", [r"(?<![a-z0-9])rd(?![a-z0-9])", r"rd_?exp", r"research_?exp", r"研发"],
     ["研发投入", "研发费用"]),
    ("mainbiz", [r"mainbiz", r"主营构成", r"收入构成"], ["主营", "收入构成", "收入占比"]),
]

_PERIOD_SEG = re.compile(r"^(?:\d{4}A?|\d{4}H1|\d{4}Q[1-4]|\d{4}-\d{2}-\d{2})$")
_ANNUAL_SEG = re.compile(r"^\d{4}A?$")


def _classify(key: str, content: str = "") -> Optional[str]:
    """指标键 (+内容兜底) → 类别; 无法识别返回 None"""
    kl = (key or "").lower()
    for cat, kregs, _kw in _RULES:
        if any(re.search(r_, kl) for r_ in kregs):
            return cat
    for cat, _kregs, ckw in _RULES:
        if any(kw and kw in content for kw in ckw):
            return cat
    return None


def _series_key(f: Fact) -> str:
    """eid 去前缀/去年报首段年份/去期间 → 指标键:
    T:tushare.revenue.2024A -> 'tushare.revenue';  A:2024.mdna.3.1 -> 'mdna.3.1'"""
    body = f.eid[2:] if f.eid.startswith(("T:", "A:")) else f.eid
    parts = body.split(".")
    if f.eid.startswith("A:") and parts and re.match(r"^\d{4}$", parts[0]):
        parts = parts[1:]
    if len(parts) > 1 and _PERIOD_SEG.match(parts[-1]):
        parts = parts[:-1]
    return ".".join(parts)


@dataclass
class _Series:
    """同一指标键的年度时序 (period 'YYYY'/'YYYYA' -> Fact)"""
    key: str
    cat: str
    unit: str = ""
    points: "OrderedDict[str, Fact]" = field(default_factory=OrderedDict)

    def ymap(self) -> Dict[int, Fact]:
        return {int(p[:4]): f for p, f in self.points.items()}


def _annual_entry(f: Fact) -> Optional[Tuple[str, str]]:
    """年度数值事实 -> (指标键, 期间); 非年度/无数值返回 None。
    A: 年报 eid 首段即年份 (A:2024.mdna.3.1), 无显式 period 时取首段。"""
    if f.value is None or not f.eid.startswith(("T:", "A:")):
        return None
    period = f.period or ""
    if not period:
        body = f.eid[2:].split(".")
        if f.eid.startswith("A:") and re.match(r"^\d{4}$", body[0]):
            period = body[0]
        elif len(body) > 1 and _PERIOD_SEG.match(body[-1]):
            period = body[-1]
    if not _ANNUAL_SEG.match(period or ""):
        return None
    key = _series_key(f)
    return (key, period) if key else None


_EXCLUDE_ANNUAL = ("mainbiz", "pe", "pb")  # 走专用通道 (构成/估值)


def _annual_series(facts: List[Fact]) -> "OrderedDict[str, _Series]":
    """facts -> {指标键: 年度序列}; 同键同期以高置信优先"""
    out: "OrderedDict[str, _Series]" = OrderedDict()
    for f in sorted(facts, key=lambda x: (-x.confidence, x.eid)):
        entry = _annual_entry(f)
        if entry is None:
            continue
        key, period = entry
        cat = _classify(key, f.content)
        if not cat or cat in _EXCLUDE_ANNUAL:
            continue
        s = out.get(key)
        if s is None:
            s = out[key] = _Series(key=key, cat=cat)
        s.points.setdefault(period, f)
        if not s.unit:
            s.unit = f.unit or ""
    return out


def _best(series: "OrderedDict[str, _Series]", cat: str) -> Optional[_Series]:
    """同类别取点数最多的序列 (同指标可能有多个口径, 画点数最全的)"""
    cands = [s for s in series.values() if s.cat == cat]
    return max(cands, key=lambda s: (len(s.points), s.key)) if cands else None


# ============================================================
# 绘图小工具 (风格全部对齐 skills/content/charts.py)
# ============================================================

def _fmt(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _bar_label(ax, x: float, v: float, peak: float, text: str,
               color: str = _FG, fontsize: float = 9) -> None:
    up = v >= 0
    ax.annotate(text, (x, v + peak * 0.025 if up else v - peak * 0.025),
                ha="center", va="bottom" if up else "top",
                fontsize=fontsize, color=color)


def _note(used: List[Fact], extra: str = "") -> str:
    """图脚注: 证据 eid 溯源 (三层分离 — 只列 fact 层)"""
    uniq = list(dict.fromkeys(f.eid for f in used))
    head = "、".join(uniq[:3]) + (f" 等{len(uniq)}条" if len(uniq) > 3 else "")
    src = used[0].source or ""
    if len(src) > 44:
        src = src[:44] + "…"
    txt = f"证据溯源: {head} · 来源: {src}"
    return txt + (f" · {extra}" if extra else "")


def _legend_fg(leg) -> None:
    for t in leg.get_texts():
        t.set_color(_FG)


def _title(name: str, text: str) -> str:
    return f"{name} · {text}" if name else text


def _twin_style(ax2) -> None:
    ax2.set_facecolor("none")
    for side in ("top", "right", "left"):
        ax2.spines[side].set_visible(False)
    ax2.spines["bottom"].set_color(_GRID)
    ax2.tick_params(colors=_DIM, labelsize=10)
    ax2.grid(visible=False)


def _ylim_headroom(ax, vals: List[float], head: float = 1.30) -> None:
    peak = max((abs(v) for v in vals), default=1) or 1
    top = max(vals + [0]) * head if vals else 1
    bottom = min(0, min(vals) * 1.35) if vals else 0
    ax.set_ylim(bottom, top if top else peak * head)


# ============================================================
# 图构建器 (每个返回 Optional[FigureOut]; 命名 figs/ch{no:02d}_{slug}.png)
# ============================================================

def _annual_pair(out_dir: Path, fname: str, chapter_no: int, hint: str, title: str,
                 note: str, s_left: _Series, label_left: str,
                 s_right: Optional[_Series], label_right: str) -> FigureOut:
    """双系列年度分组柱 (双轴, 风格同 charts.growth_chart); 左系列带同比标注(红涨绿跌)。
    财报累计口径铁律: 只用 bar, 绝不折线连点。"""
    years = sorted(set(s_left.ymap()) | (set(s_right.ymap()) if s_right else set()))
    xs = [str(y) for y in years]
    idx = list(range(len(xs)))
    w = 0.38
    fig, ax = _new_fig(FIG_WIDTH, 6.0)
    m1 = s_left.ymap()
    v1 = [float(m1[y].value) if y in m1 else None for y in years]
    x1 = [i - w / 2 for i, v in zip(idx, v1) if v is not None]
    vv1 = [v for v in v1 if v is not None]
    ax.bar(x1, vv1, width=w, color=_ACCENT,
           label=f"{label_left} ({s_left.unit or '?'})")
    peak = max((abs(v) for v in vv1), default=1) or 1
    for i, v in enumerate(v1):
        if v is None:
            continue
        _bar_label(ax, i - w / 2, v, peak, _fmt(v))
        if i and v1[i - 1]:          # 同比标注: A股红涨绿跌
            g = (v - v1[i - 1]) / abs(v1[i - 1]) * 100
            ax.annotate(f"{g:+.1f}%",
                        (i - w / 2, v + peak * 0.085 if v >= 0 else v - peak * 0.085),
                        ha="center", va="bottom" if v >= 0 else "top",
                        fontsize=8.5, color=_POS if g >= 0 else _NEG)
    handles, labels = ax.get_legend_handles_labels()
    if s_right:
        m2 = s_right.ymap()
        v2 = [float(m2[y].value) if y in m2 else None for y in years]
        x2 = [i + w / 2 for i, v in zip(idx, v2) if v is not None]
        vv2 = [v for v in v2 if v is not None]
        ax2 = ax.twinx()
        ax2.bar(x2, vv2, width=w, color=_ACCENT2,
                label=f"{label_right} ({s_right.unit or '?'})")
        peak2 = max((abs(v) for v in vv2), default=1) or 1
        for i, v in enumerate(v2):
            if v is not None:
                _bar_label(ax2, i + w / 2, v, peak2, _fmt(v))
        _twin_style(ax2)
        h2, l2 = ax2.get_legend_handles_labels()
        handles, labels = handles + h2, labels + l2
    _style_ax(ax)
    ax.set_xticks(idx, xs)
    _ylim_headroom(ax, vv1)
    leg = ax.legend(handles, labels, loc="upper left", frameon=False, fontsize=10.5)
    _legend_fg(leg)
    ax.set_title(title, color=_FG, fontsize=14, pad=14, loc="left")
    path = out_dir / fname
    _finish(fig, path, title, note=note)
    return FigureOut(path, chapter_no, hint)


def _annual_single(out_dir: Path, fname: str, chapter_no: int, hint: str, title: str,
                   note: str, s: _Series, label: str, yoy: bool = False) -> FigureOut:
    """单指标年度柱状 + 数值标注 (可选同比红涨绿跌)"""
    pairs = sorted(s.ymap().items())
    xs = [str(y) for y, _ in pairs]
    vs = [float(f.value) for _, f in pairs]
    fig, ax = _new_fig(FIG_WIDTH, 5.4)
    idx = range(len(xs))
    ax.bar(idx, vs, width=0.56, color=_ACCENT, label=f"{label} ({s.unit or '?'})")
    peak = max((abs(v) for v in vs), default=1) or 1
    for i, v in enumerate(vs):
        _bar_label(ax, i, v, peak, _fmt(v))
        if yoy and i and vs[i - 1]:
            g = (v - vs[i - 1]) / abs(vs[i - 1]) * 100
            ax.annotate(f"{g:+.1f}%",
                        (i, v + peak * 0.085 if v >= 0 else v - peak * 0.085),
                        ha="center", va="bottom" if v >= 0 else "top",
                        fontsize=8.5, color=_POS if g >= 0 else _NEG)
    _style_ax(ax)
    ax.set_xticks(list(idx), xs)
    _ylim_headroom(ax, vs)
    ax.set_title(title, color=_FG, fontsize=14, pad=14, loc="left")
    path = out_dir / fname
    _finish(fig, path, title, note=note)
    return FigureOut(path, chapter_no, hint)


def _fig_growth(series, name, out_dir, allowed):
    if not _allow(4, allowed):
        return None
    rev = _best(series, "revenue")
    prof = _best(series, "profit")
    cands = [s for s in (rev, prof) if s and len(s.points) >= 2]
    if not cands:
        return None
    left, right = cands[0], (cands[1] if len(cands) > 1 else None)
    label_l = "营业收入" if left.cat == "revenue" else "归母净利润"
    used = list(left.points.values()) + (list(right.points.values()) if right else [])
    return _annual_pair(
        out_dir, "ch04_growth.png", 4,
        "章末插入 · 营收与归母净利润年度趋势 (同比红涨绿跌)",
        _title(name, f"营收与归母净利润 (年度)"),
        _note(used, "年报合并口径"),
        left, label_l, right, "归母净利润")


def _fig_margins(series, name, out_dir, allowed):
    if not _allow(4, allowed):
        return None
    order = [("gross", "毛利率", _ACCENT), ("netmargin", "净利率", _ACCENT2),
             ("roe", "ROE", _POS)]
    picks = []
    for cat, label, color in order:
        s = _best(series, cat)
        if s and len(s.points) >= 2:
            picks.append((label, color, s))
    if not picks:
        return None
    years = sorted(set().union(*[set(s.ymap()) for _, _, s in picks]))
    xs = [str(y) for y in years]
    idx = list(range(len(xs)))
    fig, ax = _new_fig(FIG_WIDTH, 5.8)
    w = 0.78 / len(picks)
    used = []
    for i, (label, color, s) in enumerate(picks):
        m = s.ymap()
        vals = [float(m[y].value) if y in m else None for y in years]
        xs_ = [x + (i - (len(picks) - 1) / 2) * w for x, v in zip(idx, vals)
               if v is not None]
        vs_ = [v for v in vals if v is not None]
        ax.bar(xs_, vs_, width=w * 0.9, color=color, label=label)
        peak = max((abs(v) for v in vs_), default=1) or 1
        for x, v in zip(xs_, vs_):
            _bar_label(ax, x, v, peak, _fmt(v), fontsize=8.5)
        used += list(s.points.values())
    _style_ax(ax)
    ax.set_xticks(idx, xs)
    _ylim_headroom(ax, [float(f.value) for _, _, s in picks for f in s.points.values()])
    leg = ax.legend(loc="upper left", frameon=False, fontsize=10.5)
    _legend_fg(leg)
    ax.set_title(_title(name, "盈利质量: 毛利率 / 净利率 / ROE (年度, %)"),
                 color=_FG, fontsize=14, pad=14, loc="left")
    path = out_dir / "ch04_margins.png"
    _finish(fig, path, "盈利质量", note=_note(used, "财务指标口径"))
    return FigureOut(path, 4, "章末插入 · 盈利质量指标趋势 (毛利率/净利率/ROE)")


def _fig_np_ocf(series, name, out_dir, allowed):
    if not _allow(4, allowed):
        return None
    prof = _best(series, "profit")
    ocf = _best(series, "ocf")
    if not (prof and ocf and len(prof.points) >= 2 and len(ocf.points) >= 2):
        return None
    used = list(prof.points.values()) + list(ocf.points.values())
    # 现金流/净利 匹配度 (最近共同年份) 写进脚注, 佐证盈利质量
    mp, mo = prof.ymap(), ocf.ymap()
    common = sorted(set(mp) & set(mo))
    extra = ""
    if common and float(mp[common[-1]].value):
        ratio = float(mo[common[-1]].value) / float(mp[common[-1]].value)
        extra = f"{common[-1]}年经营现金流/归母净利={ratio:.2f}"
    return _annual_pair(
        out_dir, "ch04_np_ocf.png", 4,
        "章末插入 · 净利润与经营现金流匹配 (盈利质量)",
        _title(name, "归母净利润与经营活动现金流净额 (年度)"),
        _note(used, extra),
        prof, "归母净利润", ocf, "经营现金流净额")


def _fig_volume(series, name, out_dir, allowed):
    if not _allow(5, allowed):
        return None
    vol = _best(series, "volume")
    sal = _best(series, "salesvol")
    cands = [s for s in (vol, sal) if s and len(s.points) >= 2]
    if not cands:
        return None
    left, right = cands[0], (cands[1] if len(cands) > 1 else None)
    label_l = "产量" if left.cat == "volume" else "销量"
    used = list(left.points.values()) + (list(right.points.values()) if right else [])
    return _annual_pair(
        out_dir, "ch05_volume.png", 5,
        "章末插入 · 产销量趋势 (经营驱动因子)",
        _title(name, f"{label_l}" + ("与销量" if right else "") + " (年度)"),
        _note(used, "年报披露口径"),
        left, label_l, right, "销量")


def _fig_util(series, name, out_dir, allowed):
    if not _allow(5, allowed):
        return None
    s = _best(series, "util")
    if not (s and len(s.points) >= 2):
        return None
    return _annual_single(
        out_dir, "ch05_util.png", 5,
        "章末插入 · 产能利用率 (经营驱动因子)",
        _title(name, "产能利用率 (年度)"),
        _note(list(s.points.values())),
        s, "产能利用率")


def _fig_rd(series, name, out_dir, allowed):
    if not _allow(6, allowed):
        return None
    s = _best(series, "rd")
    if not (s and len(s.points) >= 2):
        return None
    return _annual_single(
        out_dir, "ch06_rd.png", 6,
        "章末插入 · 研发投入趋势 (技术与竞争优势)",
        _title(name, "研发投入 (年度)"),
        _note(list(s.points.values())),
        s, "研发投入", yoy=True)


# ---------- 收入构成 (占比条形, ch02) ----------

def _item_name(f: Fact) -> str:
    """构成项名: 优先 eid 中 mainbiz 段之后的部分, 兜底从 content 掐头"""
    parts = _series_key(f).split(".")
    for i, p in enumerate(parts):
        if "mainbiz" in p.lower() or "主营" in p or "收入构成" in p:
            tail = parts[i + 1:]
            if tail:
                return ".".join(tail)[:12]
            break
    seg = re.split(r"收入占比|占比|收入构成|营业收入", f.content or "")[0]
    seg = re.sub(r"^20\d{2}年?", "", seg).strip("，。 :：、")
    return (seg or f.eid[-12:])[:12]


def _mainbiz_items(facts: List[Fact]) -> List[Tuple[str, float, Fact]]:
    """mainbiz 类事实按期间分组, 取条目最多的期间 -> [(项名, 占比%, Fact)] 降序"""
    by_period: Dict[str, List[Fact]] = {}
    for f in facts:
        if f.value is None:
            continue
        if _classify(_series_key(f), f.content) == "mainbiz":
            by_period.setdefault(f.period or "", []).append(f)
    if not by_period:
        return []
    period, group = max(by_period.items(), key=lambda kv: (len(kv[1]), kv[0]))
    items = [(_item_name(f), float(f.value), f) for f in group]
    if items and max(v for _, v, _ in items) <= 1.5:   # 小数占比归一成 %
        items = [(n, v * 100, f) for n, v, f in items]
    items.sort(key=lambda t: -t[1])
    return items


def _fig_mainbiz(facts, name, out_dir, allowed):
    if not _allow(2, allowed):
        return None
    items = _mainbiz_items(facts)
    if len(items) < 2:
        return None
    items = items[:6]
    rows = sorted(items, key=lambda t: t[1])           # barh: 最大在顶部
    names = [n for n, _, _ in rows]
    shares = [v for _, v, _ in rows]
    used = [f for _, _, f in rows]
    fig, ax = _new_fig(FIG_WIDTH, 1.1 + 0.75 * len(rows))
    bars = ax.barh(names, shares, color=_ACCENT, height=0.62, edgecolor="none")
    for b, s in zip(bars, shares):
        ax.text(b.get_width() + max(shares) * 0.015,
                b.get_y() + b.get_height() / 2, f"{s:.1f}%",
                va="center", fontsize=10.5, color=_FG)
    _style_ax(ax)
    ax.grid(axis="x", color=_GRID, linewidth=0.6, alpha=0.6)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(shares) * 1.38)
    period = used[0].period or ""
    ax.set_title(_title(name, f"收入构成: 主营业务收入占比 ({period})"),
                 color=_FG, fontsize=14, pad=14, loc="left")
    path = out_dir / "ch02_mainbiz.png"
    _finish(fig, path, "收入构成", note=_note(used, "占主营收入比重"))
    return FigureOut(path, 2, "章末插入 · 收入构成占比 (佐证业务拆解)")


# ---------- 估值水位 (区间带, ch10) ----------

@dataclass
class _ValRow:
    label: str
    vals: List[float]          # 升序样本
    latest: float
    latest_fact: Fact
    sample: List[Fact]         # 时间升序


def _valuation_rows(facts: List[Fact]) -> List[_ValRow]:
    rows = []
    for cat, label in (("pe", "PE(TTM)"), ("pb", "PB")):
        group = [f for f in facts if f.value is not None
                 and _classify(_series_key(f), f.content) == cat]
        pts: Dict[str, Fact] = {}
        for f in sorted(group, key=lambda x: -x.confidence):
            if f.period:
                pts.setdefault(f.period, f)
        if len(pts) < 3:
            continue
        sample = [pts[k] for k in sorted(pts)]
        vals = sorted(float(f.value) for f in sample)
        rows.append(_ValRow(label=label, vals=vals,
                            latest=float(sample[-1].value),
                            latest_fact=sample[-1], sample=sample))
    return rows


def _fig_valuation(facts, name, out_dir, allowed):
    if not _allow(10, allowed):
        return None
    rows = _valuation_rows(facts)
    if not rows:
        return None
    fig, ax = _new_fig(FIG_WIDTH, 3.4)
    used = []
    for i, r in enumerate(rows):
        y = len(rows) - 1 - i
        ax.hlines(y, r.vals[0], r.vals[-1], color=_GRID, linewidth=10)
        med = r.vals[len(r.vals) // 2]
        for p in (r.vals[0], med, r.vals[-1]):
            ax.plot(p, y, "o", color=_DIM, markersize=5)
        ax.plot(r.latest, y, "D", color=_POS, markersize=11, zorder=5)
        pct = sum(1 for v in r.vals if v <= r.latest) / len(r.vals) * 100
        ax.annotate(f"{r.label} {r.latest:.1f} ({pct:.0f}% 分位)",
                    (r.latest, y), textcoords="offset points", xytext=(0, 16),
                    ha="center", fontsize=11.5, color=_FG)
        ax.annotate(f"低 {r.vals[0]:.1f}", (r.vals[0], y), xytext=(0, -14),
                    textcoords="offset points", fontsize=9, color=_DIM)
        ax.annotate(f"高 {r.vals[-1]:.1f}", (r.vals[-1], y), xytext=(0, -14),
                    textcoords="offset points", fontsize=9, color=_DIM,
                    ha="right")
        used += r.sample
    ax.set_ylim(-0.7, len(rows) - 0.2)
    ax.set_yticks([])
    _style_ax(ax)
    ax.grid(visible=False)
    ax.set_title(_title(name, "估值水位: 当前值处于样本区间的位置 (◆)"),
                 color=_FG, fontsize=14, pad=26, loc="left")
    span = f"{rows[0].sample[0].period}~{rows[0].sample[-1].period}"
    # 脚注放独立底边带, 与 x 轴刻度错层 (证据 eid 列表较长, 避免与刻度数字叠压)
    fig.subplots_adjust(bottom=0.30)
    fig.text(0.99, 0.04, _note(used, f"样本区间 {span}"),
             ha="right", va="bottom", fontsize=8.5, color=_DIM)
    path = out_dir / "ch10_valuation.png"
    _finish(fig, path, "估值水位", note="")
    return FigureOut(path, 10, "章末插入 · 估值水位 (PE/PB 区间位置)")


# ============================================================
# 总入口
# ============================================================

def _allow(no: int, allowed: Optional[set]) -> bool:
    return allowed is None or no in allowed


def build_figures(draft: Draft, facts: List[Fact], research_dir: Path
                  ) -> List[FigureOut]:
    """证据时序 → 章节配图清单 (figs/ch{no:02d}_{slug}.png)。

    - 只消费 fact 层; 每张图可溯源到 facts (图脚注列 eid)
    - draft.chapters 非空时只产出能落进现有章的图 (空 draft 全放行, 便于先行开发)
    - 返回按 (章号, 文件名) 排序; 不改 draft.json, 插入由 T08 组装
    """
    allowed = {ch.no for ch in draft.chapters} if (draft and draft.chapters) else None
    name = (draft.name if draft else "") or ""
    out_dir = Path(research_dir) / _FIGS_SUBDIR
    series = _annual_series(facts)
    makers = (
        (_fig_mainbiz, (facts,)),
        (_fig_growth, (series,)),
        (_fig_margins, (series,)),
        (_fig_np_ocf, (series,)),
        (_fig_volume, (series,)),
        (_fig_util, (series,)),
        (_fig_rd, (series,)),
        (_fig_valuation, (facts,)),
    )
    figs: List[FigureOut] = []
    for fn, args in makers:
        r = fn(*args, name=name, out_dir=out_dir, allowed=allowed)
        if r is not None:
            figs.append(r)
            logger.info(f"配图 ch{r.chapter_no:02d} {r.path.name} <- {r.hint}")
    figs.sort(key=lambda f: (f.chapter_no, f.path.name))
    return figs


# ============================================================
# CLI: python -m skills.research.compose.figures 600519.SH
# ============================================================

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    from core.paths import PathManager
    from skills.research.contracts import Draft as _Draft
    from skills.research.contracts import load_draft, load_pack

    ap = argparse.ArgumentParser(
        prog="python -m skills.research.compose.figures",
        description="研究配图工场: 读取研究目录下的证据分片 (+draft) → 章节配图 figs/")
    ap.add_argument("stock", help="股票代码, 如 600519.SH")
    ap.add_argument("--figs-only", action="store_true",
                    help="忽略 draft 章节过滤, 证据允许的图全出")
    args = ap.parse_args(argv)

    rdir = Path(PathManager().output_dir) / "research" / args.stock
    if not rdir.exists():
        print(f"未找到研究目录: {rdir}")
        return 1
    facts, views = load_pack(rdir)
    dpath = rdir / "draft.json"
    draft = load_draft(dpath) if dpath.exists() else _Draft(ts_code=args.stock,
                                                             name=args.stock)
    if args.figs_only:
        draft.chapters = []
    figs = build_figures(draft, facts, rdir)

    print(f"研究目录: {rdir}")
    print(f"证据: {len(facts)} 条事实 / {len(views)} 条观点"
          + ("" if dpath.exists() else " (无 draft.json, 按证据全量出图)"))
    for f in figs:
        print(f"  ch{f.chapter_no:02d}  {f.path.name}  — {f.hint}")
    if not figs:
        print("  (无可绘制的时序证据 — 图表至少需要同指标多年/多期数值事实)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
