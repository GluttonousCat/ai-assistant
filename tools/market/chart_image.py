"""Static email charts from the same adjusted OHLC/indicators used by the UI."""
from io import BytesIO
from pathlib import Path

import numpy as np

DIGEST_DAILY_BARS = 96
MINUTE_BARS = 134


def render_pair(daily, minute, *, stop=None):
    panels = ((daily, "日K · 近5年历史", None, True),
              (minute, "60F · 趋势与局部触发", MINUTE_BARS, False))
    return _render(panels, stop=stop)


def render_digest_chart(detail, timeframe, *, stop=None):
    daily = timeframe == "1D"
    title = "日K · 近期走势" if daily else "60F · 趋势与局部触发"
    overview = detail["data"] if daily and len(detail["data"]) > DIGEST_DAILY_BARS else ()
    return _render(((detail, title, DIGEST_DAILY_BARS if daily else MINUTE_BARS, daily),),
                   stop=stop, overview=overview)


def _render(panels, *, stop=None, overview=()):
    # Import only in offline jobs: importing market APIs never initializes plots.
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import LineCollection, PolyCollection
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties

    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = FontProperties(fname=str(font_path)) if font_path.exists() else FontProperties(family="DejaVu Sans")
    single = len(panels) == 1
    height = (6.2 if overview else 5) if single else 9
    fig = Figure(figsize=(14, height), dpi=110, facecolor="#11100f")
    canvas = FigureCanvasAgg(fig)
    ratios = [3.8, .85] * len(panels) + ([1.05] if overview else [])
    grid = fig.add_gridspec(len(ratios), 1, height_ratios=ratios, hspace=.5 if overview else .34,
                           left=.045, right=.89, top=.91 if single else .95,
                           bottom=.11 if single else .065)
    for slot, (detail, title, size, daily) in enumerate(panels):
        rows = detail["data"] if size is None else detail["data"][-size:]
        if not rows:
            raise ValueError("图表没有可用行情")
        ax = fig.add_subplot(grid[slot * 2])
        volume = fig.add_subplot(grid[slot * 2 + 1], sharex=ax)
        for axis in (ax, volume):
            axis.set_facecolor("#11100f")
            axis.yaxis.tick_right()
            axis.tick_params(colors="#a29a90", labelsize=10, length=0, pad=8)
            axis.grid(axis="y", color="#29251f", linewidth=.5)
            axis.set_axisbelow(True)
            for spine in axis.spines.values():
                spine.set_visible(False)
        x = np.arange(len(rows))
        opening, high, low, close = [np.asarray([r[k] for r in rows], dtype=float) for k in ("open", "high", "low", "close")]
        if not np.isfinite(np.array([opening, high, low, close])).all():
            raise ValueError("图表价格包含非有限值")
        colors = np.where(close >= opening, "#e56d73", "#41b893")
        ax.add_collection(LineCollection([[(i, l), (i, h)] for i, l, h in zip(x, low, high)], colors=colors, linewidths=1))
        # The arrays share one validated source, unlike independently keyed feeds.
        span = max(float(high.max() - low.min()), float(close[-1]) * .01)
        bodies = []
        for i, o, c in zip(x, opening, close):
            bottom, top = min(o, c), max(o, c)
            if top - bottom < span * .0008:
                top = bottom + span * .0008
            bodies.append([(i-.29, bottom), (i-.29, top), (i+.29, top), (i+.29, bottom)])
        ax.add_collection(PolyCollection(bodies, facecolors=colors, edgecolors=colors, linewidths=.6))
        windows = detail.get("chart_averages", {}).get("windows", [8, 13, 21])
        for window, color in zip(windows, ["#dfb671", "#80b8ff", "#c6a0f6"]):
            values = [r.get(f"ma{window}") for r in rows]
            ax.plot(x, [float(v) if v is not None else np.nan for v in values], color=color, linewidth=1.15, label=f"M{window}")
        ymin, ymax = float(low.min()) - span*.14, float(high.max()) + span*.12
        if daily and stop is not None and ymin <= stop <= ymax:
            ax.axhline(stop, color="#e5a579", linewidth=.8, linestyle="--", alpha=.8)
            ax.text(.01, (stop-ymin)/(ymax-ymin), f"参考止损 {stop:.2f}", transform=ax.transAxes,
                    color="#e5a579", fontsize=9, fontproperties=font, va="bottom")
        for i, row in enumerate(rows):
            entry = daily and row.get("eligible") is True
            watch = not daily and bool(row.get("reversal_ignition") or row.get("resume_watch"))
            if entry or watch:
                kind = row.get("context_pattern") or row.get("pattern")
                color = "#80b8ff" if kind == "trend_pullback" else "#c6a0f6" if kind == "base_rebound" else "#dfb671"
                ax.scatter(i, low[i]-span*.055, color=color, s=40, marker="^" if entry else "o", zorder=6,
                           edgecolors="#11100f", linewidths=.6)
        ax.axhline(close[-1], color="#82745d", linewidth=.65, linestyle=":")
        ax.text(1.075, (close[-1]-ymin)/(ymax-ymin), f"{close[-1]:.2f}", transform=ax.transAxes,
                color="#dfb671", fontsize=11, va="center", bbox={"facecolor": "#282115", "edgecolor": "none", "pad": 3})
        ax.set_ylim(ymin, ymax)
        ax.set_xlim(-1, len(rows)+.7)
        ax.set_title(title, fontproperties=font, fontsize=13, color="#e9e1d6", loc="left", pad=12)
        ax.legend(loc="upper right", frameon=False, labelcolor="#b9b0a3", fontsize=10, ncol=3)
        ax.tick_params(labelbottom=False)
        amounts = np.asarray([float(r.get("volume") or 0)/10000 for r in rows])
        volume.bar(x, amounts, width=.65, color=colors, alpha=.7)
        volume.set_ylim(0, max(float(amounts.max())*1.25, 1))
        volume.text(.005, .80, "成交量 / 万股", transform=volume.transAxes, color="#8e867b", fontsize=9, fontproperties=font)
        ticks = np.unique(np.linspace(0, len(rows)-1, 6).astype(int))
        volume.set_xticks(ticks)
        volume.set_xticklabels([
            str(rows[i]["bar_end"])[5:16].replace("T", " ") if not daily else
            str(rows[i]["bar_end"])[:(10 if single else 7)] for i in ticks
        ])
    if overview:
        ax = fig.add_subplot(grid[-1], facecolor="#11100f")
        x = np.arange(len(overview))
        ax.vlines(x, [r["low"] for r in overview], [r["high"] for r in overview],
                  color="#514332", linewidth=.38)
        ax.plot(x, [r["close"] for r in overview], color="#ac8a55", linewidth=.8)
        ax.axvspan(len(overview) - DIGEST_DAILY_BARS - .5, len(overview) - .5, color="#564630", alpha=.35)
        ax.set_xlim(-.5, len(overview) - .5)
        ticks = np.unique(np.linspace(0, len(overview) - 1, 6).astype(int))
        ax.set_xticks(ticks, [str(overview[i]["bar_end"])[:7] for i in ticks])
        ax.set_yticks([])
        ax.tick_params(axis="x", length=0, labelsize=9, colors="#a29a90", pad=5)
        ax.set_title("日K历史概览 · 近5年（上市不足按实际）", fontproperties=font,
                     fontsize=10, color="#b9b0a3", loc="left", pad=6)
        for spine in ax.spines.values():
            spine.set_visible(False)
    if not single:
        fig.text(.045, .012, "前复权 · M8 / M13 / M21    ▲ 日K入场标记    ● 60F观察标记（非入场）",
                 color="#8f867a", fontproperties=font, fontsize=10)
    buffer = BytesIO()
    canvas.print_png(buffer)
    fig.clear()
    # Indexed color reduces the SMTP payload without resampling the chart.
    from PIL import Image
    image = Image.open(buffer).convert("RGB")
    compact = BytesIO()
    image.quantize(colors=128, method=Image.Quantize.MEDIANCUT,
                   dither=Image.Dither.NONE).save(compact, format="PNG", optimize=True)
    return compact.getvalue()
