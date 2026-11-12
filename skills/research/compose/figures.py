# -*- encoding: utf-8 -*-
"""
T07 · figure factory (Stage 3) — auto-produce chapter figures from evidence time series

build_figures(draft, facts, research_dir) -> List[FigureOut(path, chapter_no, hint)]
Never touches draft.json; returns a list of (path, chapter no, insertion-position hint)
for T08 to insert at the end of the matching chapter during assembly.

Figure-type routing (iron rule: cumulative-report figures must never be drawn as a line
joining points; the same metric across years is always bars):
  revenue mix              -> share bar-h       ch02 business model & segment breakdown (T05 wants: mainbiz)
  multi-year rev/net profit-> dual-axis grouped bars ch04 history & financial quality (wants: financials; YoY red-up/green-down)
  gross/net margin / ROE   -> grouped bars      ch04
  net profit vs op. cash flow -> grouped bars   ch04 (earnings quality: cash flow matches profit)
  production/sales volume  -> grouped bars      ch05 operating drivers & profit mechanism (wants: annual/financials)
  capacity utilization     -> bars              ch05
  R&D spend                -> bars              ch06 technology, R&D & competitive edge (wants: financials)
  valuation level          -> range band        ch10 valuation & tracking (wants: financials; quotes are not cumulative)

Style aligned with skills/content/charts.py (dark theme / Microsoft YaHei / 1080px,
import-only, never modified); each figure consumes fact-layer evidence only (three-layer
separation) with an eid-tracing footnote; A-share red-up/green-down (YoY labels).
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import matplotlib

matplotlib.use("Agg")  # pure batch-rendering module; pin the backend before any pyplot import (runs offline)

from core.logger import get_logger
from skills.content.charts import (_ACCENT, _ACCENT2, _BG, _DIM, _FG, _GRID,
                                   _NEG, _POS, _finish, _new_fig, _style_ax)
from skills.research.contracts import Draft, Fact

logger = get_logger(__name__)

FIG_WIDTH = 10.8  # official-account width 1080px (dpi=100)
_FIGS_SUBDIR = "figs"


class FigureOut(NamedTuple):
    """T07->T08 handoff artifact: image path + owning chapter no + insertion-position hint (unpackable as a 3-tuple)"""
    path: Path
    chapter_no: int
    hint: str


# ============================================================
# Evidence time-series organization: eid -> metric series
# ============================================================

# Classification rules: (category, metric-key regex [lowercase match], content keywords)
# — order is priority, more specific first (the yoy trap comes first: growth-rate facts (%)
# must never sneak into profit/revenue charts as levels (100M-yuan amounts); netmargin before
# profit so netprofit_margin isn't swallowed by "netprofit"; pe/pb use word boundaries to exclude capex)
_RULES: List[Tuple[str, List[str], List[str]]] = [
    ("yoy", [r"_?yoy$", r"_?yoy\.", r"同比"], ["同比增长", "同比"]),
    ("netmargin", [r"netprofit_?margin", r"net_?margin", r"净利率"], ["净利率"]),
    ("gross", [r"grossprofit", r"gross_?margin", r"毛利率"], ["毛利率"]),
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
    """Metric key (+ content fallback) -> category; returns None when unrecognized"""
    kl = (key or "").lower()
    for cat, kregs, _kw in _RULES:
        if any(re.search(r_, kl) for r_ in kregs):
            return cat
    for cat, _kregs, ckw in _RULES:
        if any(kw and kw in content for kw in ckw):
            return cat
    return None


def _series_key(f: Fact) -> str:
    """Strip prefix / annual leading-year segment / period from an eid -> metric key:
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
    """Annual time series of a single metric key (period 'YYYY'/'YYYYA' -> Fact)"""
    key: str
    cat: str
    unit: str = ""
    points: "OrderedDict[str, Fact]" = field(default_factory=OrderedDict)

    def ymap(self) -> Dict[int, Fact]:
        return {int(p[:4]): f for p, f in self.points.items()}


def _annual_entry(f: Fact) -> Optional[Tuple[str, str]]:
    """Annual numeric fact -> (metric key, period); returns None for non-annual / valueless facts.
    For A: eids the first segment is the year (A:2024.mdna.3.1); used when period is absent."""
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


_EXCLUDE_ANNUAL = ("mainbiz", "pe", "pb")  # handled by dedicated channels (mix / valuation)


def _annual_series(facts: List[Fact]) -> "OrderedDict[str, _Series]":
    """facts -> {metric key: annual series}; same key+period resolved by higher confidence first"""
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
    """Pick the series with the most points within a category (one metric may exist under several calibers; draw the most complete one)"""
    cands = [s for s in series.values() if s.cat == cat]
    return max(cands, key=lambda s: (len(s.points), s.key)) if cands else None


# ============================================================
# Drawing helpers (style fully aligned with skills/content/charts.py)
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
    """Figure footnote: evidence eid tracing (three-layer separation — fact layer only)"""
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


_BG_RGB = tuple(int(_BG[i:i + 2], 16) for i in (1, 3, 5))   # "#241f1c" -> (36,31,28)


def _finish1080(fig, path: Path, title: str, note: str = "") -> Path:
    """After _finish (tight crop) the width is undetermined (may be narrower or wider)
    -> normalize to the official-account standard width of 1080px: pad with the
    background color and center when narrower; scale down proportionally when wider
    (labels drawn outside the canvas)."""
    p = _finish(fig, path, title, note=note)
    try:
        from PIL import Image
        with Image.open(p) as img:
            img.load()
            w, h = img.size
            if w == _CANVAS_W or h == 0:
                return p
            if w > _CANVAS_W:
                out = img.resize((_CANVAS_W, round(h * _CANVAS_W / w)),
                                 Image.LANCZOS)
            else:
                out = Image.new("RGB", (_CANVAS_W, h), _BG_RGB)
                out.paste(img, ((_CANVAS_W - w) // 2, 0))
        out.save(p)
    except ImportError:                 # without Pillow keep the tight-crop width (only a size mismatch, never blocking)
        logger.warning("Pillow 缺失, 跳过 1080px 宽度统一")
    return p


_CANVAS_W = int(FIG_WIDTH * 100)        # 1080


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
# Figure builders (each returns Optional[FigureOut]; named figs/ch{no:02d}_{slug}.png)
# ============================================================

def _annual_pair(out_dir: Path, fname: str, chapter_no: int, hint: str, title: str,
                 note: str, s_left: _Series, label_left: str,
                 s_right: Optional[_Series], label_right: str) -> FigureOut:
    """Two-series annual grouped bars (dual axis, style as charts.growth_chart); the left
    series carries YoY labels (red-up/green-down). Iron rule for cumulative report
    figures: bars only, never connect points with a line."""
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
        if i and v1[i - 1]:          # YoY label: A-share red-up/green-down
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
    _finish1080(fig, path, title, note=note)
    return FigureOut(path, chapter_no, hint)


def _annual_single(out_dir: Path, fname: str, chapter_no: int, hint: str, title: str,
                   note: str, s: _Series, label: str, yoy: bool = False) -> FigureOut:
    """Single-metric annual bars + value labels (optional YoY red-up/green-down)"""
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
    _finish1080(fig, path, title, note=note)
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
    _finish1080(fig, path, "盈利质量", note=_note(used, "财务指标口径"))
    return FigureOut(path, 4, "章末插入 · 盈利质量指标趋势 (毛利率/净利率/ROE)")


def _fig_np_ocf(series, name, out_dir, allowed):
    if not _allow(4, allowed):
        return None
    prof = _best(series, "profit")
    ocf = _best(series, "ocf")
    if not (prof and ocf and len(prof.points) >= 2 and len(ocf.points) >= 2):
        return None
    used = list(prof.points.values()) + list(ocf.points.values())
    # cash-flow/net-profit match (latest common year) goes into the footnote as earnings-quality evidence
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


# ---------- revenue mix (share bar-h, ch02) ----------

_MB_NAME_RE = re.compile(r"\)\s*([^()（）]+?)\s*[（(]主营构成")   # "2026中报(…) 茅台酒 (主营构成…"
_MB_SHARE_RE = re.compile(r"占[^\d%]{0,8}([\d.]+)\s*%")          # "占营业收入 73.87%"
_MB_GM_RE = re.compile(r"毛利率\s*([\d.]+)\s*%")


def _item_name(f: Fact) -> str:
    """Segment-item name: prefer the '…) item-name (主营构成' pattern in content; fall back to the eid mainbiz segment / content head"""
    m = _MB_NAME_RE.search(f.content or "")
    if m and m.group(1).strip():
        return m.group(1).strip()[:12]
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


def _mainbiz_items(facts: List[Fact]) -> List[Tuple[str, float, Optional[float], Fact]]:
    """Group mainbiz facts by period and take the period with the most entries
    -> [(item name, share %, gross margin %|None, Fact)] sorted by share desc.
    Three-tier share source: take「占营业收入 X%」directly from content -> normalize
    amounts against the group total (100M-yuan amount facts) -> decimal shares x100."""
    by_period: Dict[str, List[Fact]] = {}
    for f in facts:
        if f.value is None:
            continue
        if _classify(_series_key(f), f.content) == "mainbiz":
            by_period.setdefault(f.period or "", []).append(f)
    if not by_period:
        return []
    _period, group = max(by_period.items(), key=lambda kv: (len(kv[1]), kv[0]))

    def share_of(f: Fact) -> Optional[float]:
        m = _MB_SHARE_RE.search(f.content or "")
        if m:
            v = float(m.group(1))
            if 0 < v <= 100:
                return v
        return None

    shares = [share_of(f) for f in group]
    if all(s is not None for s in shares) and 80 <= sum(shares) <= 120:
        pass                                   # shares taken directly: all present and magnitudes sane
    elif group[0].unit and any(k in group[0].unit for k in ("亿", "万", "元")):
        total = sum(abs(float(f.value)) for f in group)          # amounts -> normalized
        shares = ([float(f.value) / total * 100 for f in group] if total > 0
                  else shares)
    shares = [s if s is not None else float(f.value) for s, f in zip(shares, group)]
    if shares and max(shares) <= 1.5:           # decimal-share fallback
        shares = [s * 100 for s in shares]

    items = []
    for f, s in zip(group, shares):
        gm = _MB_GM_RE.search(f.content or "")
        items.append((_item_name(f), float(s),
                      float(gm.group(1)) if gm else None, f))
    items.sort(key=lambda t: -t[1])
    return items


def _fig_mainbiz(facts, name, out_dir, allowed):
    if not _allow(2, allowed):
        return None
    items = _mainbiz_items(facts)
    if len(items) < 2:
        return None
    items = items[:6]
    rows = sorted(items, key=lambda t: t[1])           # barh: largest on top
    names = [n for n, _, _, _ in rows]
    shares = [v for _, v, _, _ in rows]
    used = [f for _, _, _, f in rows]
    fig, ax = _new_fig(FIG_WIDTH, 1.1 + 0.75 * len(rows))
    bars = ax.barh(names, shares, color=_ACCENT, height=0.62, edgecolor="none")
    for b, s, (_, _, gm, _) in zip(bars, shares, rows):
        label = f"{s:.1f}%"
        if gm is not None:
            label += f"  (毛利率 {gm:.1f}%)"
        ax.text(b.get_width() + max(shares) * 0.015,
                b.get_y() + b.get_height() / 2, label,
                va="center", fontsize=10.5, color=_FG)
    _style_ax(ax)
    ax.grid(axis="x", color=_GRID, linewidth=0.6, alpha=0.6)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(shares) * 1.38)
    period = used[0].period or ""
    ax.set_title(_title(name, f"收入构成: 主营业务收入占比 ({period})"),
                 color=_FG, fontsize=14, pad=14, loc="left")
    path = out_dir / "ch02_mainbiz.png"
    _finish1080(fig, path, "收入构成", note=_note(used, "占主营收入比重"))
    return FigureOut(path, 2, "章末插入 · 收入构成占比 (佐证业务拆解)")


# ---------- valuation level (range band, ch10) ----------

@dataclass
class _ValRow:
    label: str
    low: float
    high: float
    median: float
    latest: float
    pct: float                 # percentile %
    span: str                  # sample-range description
    used: List[Fact]


_VAL_PCT_RE = re.compile(r"([\d.]+)\s*%\s*分位")
_VAL_RANGE_RE = re.compile(r"区间\s*([\d.]+)\s*~\s*([\d.]+)")
_VAL_SPAN_RE = re.compile(r"(近\s*\d+\s*年)")


def _valuation_rows(facts: List[Fact]) -> List[_ValRow]:
    """One row per metric, two possible sources:
    1) distribution mode: valuation facts across >=3 distinct periods -> min/median/max
       and latest percentile all computed;
    2) parse mode: a single fact whose content embeds「区间 X~Y … N% 分位」(T02 profile valuation block)."""
    rows = []
    for cat, label in (("pe", "PE(TTM)"), ("pb", "PB")):
        group = [f for f in facts if f.value is not None
                 and _classify(_series_key(f), f.content) == cat]
        pts: Dict[str, Fact] = {}
        for f in sorted(group, key=lambda x: -x.confidence):
            if f.period:
                pts.setdefault(f.period, f)
        if len(pts) >= 3:                              # 1) distribution mode
            sample = [pts[k] for k in sorted(pts)]
            vals = sorted(float(f.value) for f in sample)
            latest = float(sample[-1].value)
            rows.append(_ValRow(
                label=label, low=vals[0], high=vals[-1],
                median=vals[len(vals) // 2], latest=latest,
                pct=sum(1 for v in vals if v <= latest) / len(vals) * 100,
                span=f"{sample[0].period}~{sample[-1].period}", used=sample))
            continue
        for f in sorted(group, key=lambda x: -x.confidence):    # 2) parse mode
            mr, mp = _VAL_RANGE_RE.search(f.content or ""), _VAL_PCT_RE.search(f.content or "")
            if not (mr and mp):
                continue
            low, high = float(mr.group(1)), float(mr.group(2))
            ms = _VAL_SPAN_RE.search(f.content or "")
            rows.append(_ValRow(
                label=label, low=low, high=high,
                median=(low + high) / 2, latest=float(f.value),
                pct=float(mp.group(1)),
                span=ms.group(1).replace(" ", "") if ms else "样本区间",
                used=[f]))
            break
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
        ax.hlines(y, r.low, r.high, color=_GRID, linewidth=10)
        for p in (r.low, r.median, r.high):
            ax.plot(p, y, "o", color=_DIM, markersize=5)
        ax.plot(r.latest, y, "D", color=_POS, markersize=11, zorder=5)
        ax.annotate(f"{r.label} {r.latest:.1f} ({r.pct:.0f}% 分位)",
                    (r.latest, y), textcoords="offset points", xytext=(0, 16),
                    ha="center", fontsize=11.5, color=_FG)
        ax.annotate(f"低 {r.low:.1f}", (r.low, y), xytext=(0, -14),
                    textcoords="offset points", fontsize=9, color=_DIM)
        ax.annotate(f"高 {r.high:.1f}", (r.high, y), xytext=(0, -14),
                    textcoords="offset points", fontsize=9, color=_DIM,
                    ha="right")
        used += r.used
    ax.set_ylim(-0.7, len(rows) - 0.2)
    ax.set_yticks([])
    _style_ax(ax)
    ax.grid(visible=False)
    ax.set_title(_title(name, "估值水位: 当前值处于样本区间的位置 (◆)"),
                 color=_FG, fontsize=14, pad=26, loc="left")
    span = rows[0].span
    # footnote on a dedicated bottom band, offset from the x-axis ticks (the eid list is long; avoid overlapping tick numbers)
    fig.subplots_adjust(bottom=0.30)
    fig.text(0.99, 0.04, _note(used, f"样本区间 {span}"),
             ha="right", va="bottom", fontsize=8.5, color=_DIM)
    path = out_dir / "ch10_valuation.png"
    _finish1080(fig, path, "估值水位", note="")
    return FigureOut(path, 10, "章末插入 · 估值水位 (PE/PB 区间位置)")


# ============================================================
# main entry
# ============================================================

def _allow(no: int, allowed: Optional[set]) -> bool:
    return allowed is None or no in allowed


def build_figures(draft: Draft, facts: List[Fact], research_dir: Path
                  ) -> List[FigureOut]:
    """Evidence time series -> chapter figure list (figs/ch{no:02d}_{slug}.png).

    - Consumes the fact layer only; every figure traces back to facts (footnote lists eids)
    - With non-empty draft.chapters, only figures fitting existing chapters are produced
      (an empty draft allows everything, so development can run ahead of the draft)
    - Returns sorted by (chapter no, filename); draft.json is untouched — T08 does the insertion
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
