# -*- encoding: utf-8 -*-
"""
文章配图生成 (matplotlib 静态 PNG, 公众号直用)

5 张图 (全部真实数据, 图注带口径与来源):
  1. mainbiz_chart      主营构成水平条形图 (占比+毛利率双标注)
  2. growth_chart       营收/归母净利双轴柱状图 (年度)
  3. margin_chart       毛利率/净利率/ROE 折线 (年度)
  4. valuation_chart    PE/PB 三年分位标尺 (横轴刻度条)
  5. industry_chart     行业内对比条形图 (本股高亮)

风格: 深色底 + 品牌暖色 (与平台深黑暗棕主题一致); 中文字体微软雅黑;
统一 1200px 宽 (公众号两倍清晰度), 输出 output/articles/assets/。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger(__name__)

ASSET_ROOT = Path("output/articles/assets")

# 品牌配色 (深色底)
_BG = "#241f1c"        # 深黑暗棕底
_FG = "#e8e0d8"        # 主文字 (暖白)
_DIM = "#9a8f85"       # 次要文字
_GRID = "#3a332e"      # 网格
_ACCENT = "#d99178"    # 主色 (品牌棕红)
_ACCENT2 = "#e0b64f"   # 辅色 (金)
_POS = "#f43f5e"       # 强调 (红)
_NEG = "#10b981"       # 次强调 (绿)
_FAMILY = ["Microsoft YaHei"]

_FONT_SET = False


def _style_ax(ax) -> None:
    ax.set_facecolor(_BG)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_GRID)
    ax.tick_params(colors=_DIM, labelsize=10)
    ax.grid(axis="y", color=_GRID, linewidth=0.6, alpha=0.6)
    ax.set_axisbelow(True)


def _new_fig(width=12.0, height=6.0, nrows=1):
    import matplotlib.pyplot as plt
    global _FONT_SET
    if not _FONT_SET:
        plt.rcParams["font.sans-serif"] = _FAMILY
        plt.rcParams["axes.unicode_minus"] = False
        _FONT_SET = True
    fig, ax = plt.subplots(figsize=(width, height), dpi=100, nrows=nrows)
    fig.patch.set_facecolor(_BG)
    return fig, ax


def _finish(fig, path: Path, title: str, note: str = "") -> Path:
    import matplotlib.pyplot as plt
    path.parent.mkdir(parents=True, exist_ok=True)
    if note:
        fig.text(0.99, 0.01, note, ha="right", va="bottom",
                 fontsize=8.5, color=_DIM)
    fig.savefig(path, facecolor=_BG, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    logger.info(f"配图已生成: {path.name}")
    return path


def _slug(ts_code: str) -> str:
    return (ts_code or "stock").replace(".", "_")


# ============================================================
# 1. 主营构成
# ============================================================

def mainbiz_chart(mainbiz: Dict[str, Any], ts_code: str) -> Optional[Path]:
    """主营构成水平条形图: 占比条 + 毛利率右标注"""
    rows = [r for r in (mainbiz.get("data") or [])
            if r.get("biz_type") == "P" and r.get("bz_item")][:5]
    if not rows:
        rows = [r for r in (mainbiz.get("data") or []) if r.get("bz_item")][:5]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: float(r.get("sales_share_pct") or 0))
    names = [str(r["bz_item"])[:12] for r in rows]
    shares = [float(r.get("sales_share_pct") or 0) for r in rows]
    margins = [r.get("gross_margin_pct") for r in rows]

    fig, ax = _new_fig(12, 0.9 + 0.75 * len(rows))
    bars = ax.barh(names, shares, color=_ACCENT, height=0.62,
                   edgecolor="none")
    for b, s, m in zip(bars, shares, margins):
        label = f"{s:.1f}%"
        if m is not None:
            label += f"  (毛利率 {float(m):.1f}%)"
        ax.text(b.get_width() + max(shares) * 0.015, b.get_y() + b.get_height() / 2,
                label, va="center", fontsize=10.5, color=_FG)
    _style_ax(ax)
    ax.grid(axis="x", color=_GRID, linewidth=0.6, alpha=0.6)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(shares) * 1.38)
    ax.set_title("主营构成: 收入占比与分业务毛利率", color=_FG,
                 fontsize=14, pad=14, loc="left")
    end_date = mainbiz.get("end_date", "")
    return _finish(fig, ASSET_ROOT / f"{_slug(ts_code)}_mainbiz.png",
                   "主营构成",
                   note=f"数据: 公司定期报告披露主营构成 ({end_date}, 来源 Tushare)")


# ============================================================
# 2. 成长性 (营收/净利双轴柱状)
# ============================================================

def growth_chart(financials: Dict[str, Any], ts_code: str,
                 years: int = 10) -> Optional[Path]:
    rows = [r for r in (financials.get("data") or [])
            if str(r.get("end_date", "")).endswith("12-31")]
    rows = sorted(rows, key=lambda r: str(r.get("end_date")))[-years:]
    if not rows:
        return None
    xs = [str(r["end_date"])[:4] for r in rows]
    rev = [float(r.get("revenue") or 0) / 1e8 for r in rows]
    np_ = [float(r.get("n_income_attr_p") or 0) / 1e8 for r in rows]

    fig, ax = _new_fig(12, 6)
    idx = range(len(xs))
    w = 0.38
    ax.bar([i - w / 2 for i in idx], rev, width=w, color=_ACCENT,
           label="营业收入 (亿元)")
    ax2 = ax.twinx()
    ax2.bar([i + w / 2 for i in idx], np_, width=w, color=_ACCENT2,
            label="归母净利润 (亿元)")
    _style_ax(ax)
    ax2.set_facecolor("none")
    for side in ("top", "right", "left"):
        ax2.spines[side].set_visible(False)
    ax2.spines["bottom"].set_color(_GRID)
    ax2.tick_params(colors=_DIM, labelsize=10)
    ax2.grid(visible=False)
    ax.set_xticks(list(idx), xs)
    ax.set_title("营收与归母净利润 (年度, 亿元)", color=_FG, fontsize=14,
                 pad=14, loc="left")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    leg = ax.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False,
                    fontsize=10.5)
    for t in leg.get_texts():
        t.set_color(_FG)
    return _finish(fig, ASSET_ROOT / f"{_slug(ts_code)}_growth.png", "成长性",
                   note="数据: 公司年报 (合并口径, 来源 Tushare)")


# ============================================================
# 3. 盈利质量折线 (毛利率/净利率/ROE)
# ============================================================

def margin_chart(financials: Dict[str, Any], ts_code: str,
                 years: int = 10) -> Optional[Path]:
    rows = [r for r in (financials.get("data") or [])
            if str(r.get("end_date", "")).endswith("12-31")]
    rows = sorted(rows, key=lambda r: str(r.get("end_date")))[-years:]
    series = {}
    for key, label, color in (("grossprofit_margin", "毛利率", _ACCENT),
                              ("netprofit_margin", "净利率", _ACCENT2),
                              ("roe", "ROE", _POS)):
        vals = [(str(r["end_date"])[:4], r.get(key)) for r in rows
                if r.get(key) is not None]
        if vals:
            series[label] = ([v[0] for v in vals],
                             [float(v[1]) for v in vals], color)
    if not series:
        return None
    fig, ax = _new_fig(12, 5.6)
    for label, (xs, ys, color) in series.items():
        ax.plot(xs, ys, marker="o", markersize=4.5, linewidth=2.2,
                color=color, label=label)
        ax.annotate(f"{ys[-1]:.1f}%", (xs[-1], ys[-1]),
                    textcoords="offset points", xytext=(8, 0),
                    fontsize=10, color=color)
    _style_ax(ax)
    ax.set_title("盈利质量: 毛利率 / 净利率 / ROE (年度, %)", color=_FG,
                 fontsize=14, pad=14, loc="left")
    leg = ax.legend(loc="upper left", frameon=False, fontsize=10.5)
    for t in leg.get_texts():
        t.set_color(_FG)
    return _finish(fig, ASSET_ROOT / f"{_slug(ts_code)}_margin.png", "盈利质量",
                   note="数据: 公司年报财务指标 (来源 Tushare)")


# ============================================================
# 4. 估值分位标尺 (PE/PB 当前值在三年区间的位置)
# ============================================================

def valuation_chart(valuation: Dict[str, Any], ts_code: str) -> Optional[Path]:
    items = [(k.upper(), v) for k, v in
             (("pe(ttm)", valuation.get("pe_ttm")), ("pb", valuation.get("pb")))
             if v and v.get("pct_rank") is not None]
    if not items:
        return None
    fig, ax = _new_fig(12, 3.2)
    for i, (label, v) in enumerate(items):
        y = len(items) - 1 - i
        ax.hlines(y, v["min"], v["max"], color=_GRID, linewidth=10)
        # 0/50/100 分位刻度点
        for p in (v["min"], (v["min"] + v["max"]) / 2, v["max"]):
            ax.plot(p, y, "o", color=_DIM, markersize=5)
        ax.plot(v["latest"], y, "D", color=_POS, markersize=11, zorder=5)
        ax.annotate(f"{label} {v['latest']}  ({v['pct_rank']}% 分位)",
                    (v["latest"], y), textcoords="offset points",
                    xytext=(0, 16), ha="center", fontsize=11.5, color=_FG)
        ax.annotate(f"低 {v['min']}", (v["min"], y), xytext=(0, -18),
                    textcoords="offset points", fontsize=9, color=_DIM)
        ax.annotate(f"高 {v['max']}", (v["max"], y), xytext=(0, -18),
                    textcoords="offset points", fontsize=9, color=_DIM,
                    ha="right")
    ax.set_ylim(-0.8, len(items) - 0.2)
    ax.set_yticks([])
    _style_ax(ax)
    ax.grid(visible=False)
    ax.set_title("估值水位: 当前值处于近 3 年区间的位置 (◆)", color=_FG,
                 fontsize=14, pad=26, loc="left")
    return _finish(fig, ASSET_ROOT / f"{_slug(ts_code)}_valuation.png", "估值",
                   note="数据: 日频估值近3年分布 (来源 Tushare)")


# ============================================================
# 5. 行业坐标 (申万同业对比, 本股高亮)
# ============================================================

def industry_chart(industry_rows: Dict[str, Any], ts_code: str,
                   name: str, metric_label: str = "ROE") -> Optional[Path]:
    """query_financials(industry) 结果 -> 本股高亮条形图"""
    data = industry_rows.get("data") or []
    rows = [(r.get("stock_name"), r.get("value")) for r in data
            if r.get("stock_name") is not None and r.get("value") is not None]
    rows = rows[:12]
    if len(rows) < 3:
        return None
    rows = sorted(rows, key=lambda x: float(x[1]))
    names = [str(n)[:8] for n, _ in rows]
    vals = [float(v) for _, v in rows]
    colors = [_ACCENT if n == name else _GRID for n in names]

    fig, ax = _new_fig(12, 0.8 + 0.62 * len(rows))
    bars = ax.barh(names, vals, color=colors, height=0.6)
    for b, v, n in zip(bars, vals, names):
        ax.text(b.get_width() + max(vals or [1]) * 0.015,
                b.get_y() + b.get_height() / 2,
                f"{v:.1f}", va="center", fontsize=10,
                color=_FG if n == name else _DIM)
    _style_ax(ax)
    ax.grid(axis="x", color=_GRID, linewidth=0.6, alpha=0.6)
    ax.grid(axis="y", visible=False)
    ax.set_title(f"行业坐标: 申万同业 {metric_label} 对比 (最新报告期)",
                 color=_FG, fontsize=14, pad=14, loc="left")
    return _finish(fig, ASSET_ROOT / f"{_slug(ts_code)}_industry.png", "行业坐标",
                   note="数据: 申万行业成分公司定期报告 (来源 Tushare)")
