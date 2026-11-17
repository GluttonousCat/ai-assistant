"""Static email charts from the same adjusted OHLC/indicators used by the UI."""
from io import BytesIO
from pathlib import Path

import numpy as np


def render_pair(daily, minute, *, stop=None):
    # Import only in offline jobs: importing market APIs never initializes plots.
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import LineCollection, PolyCollection
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties

    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = FontProperties(fname=str(font_path)) if font_path.exists() else FontProperties(family="DejaVu Sans")
    fig = Figure(figsize=(14, 9), dpi=110, facecolor="#11100f")
    canvas = FigureCanvasAgg(fig)
    grid = fig.add_gridspec(4, 1, height_ratios=[3.8, .85, 3.8, .85], hspace=.34,
                           left=.045, right=.89, top=.95, bottom=.065)
    for slot, (detail, title, size) in enumerate([(daily, "日K · 日级执行", 107), (minute, "60F · 趋势与局部触发", 134)]):
        rows = detail["data"][-size:]
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
        if slot == 0 and stop is not None and ymin <= stop <= ymax:
            ax.axhline(stop, color="#e5a579", linewidth=.8, linestyle="--", alpha=.8)
            ax.text(.01, (stop-ymin)/(ymax-ymin), f"参考止损 {stop:.2f}", transform=ax.transAxes,
                    color="#e5a579", fontsize=9, fontproperties=font, va="bottom")
        for i, row in enumerate(rows):
            entry = slot == 0 and row.get("eligible") is True
            watch = slot == 1 and bool(row.get("reversal_ignition") or row.get("resume_watch"))
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
        volume.set_xticklabels([str(rows[i]["bar_end"])[5:10] if slot == 0 else
                                str(rows[i]["bar_end"])[5:16].replace("T", " ") for i in ticks])
    fig.text(.045, .012, "前复权 · M8 / M13 / M21    ▲ 日K入场标记    ● 60F观察标记（非入场）", color="#8f867a", fontproperties=font, fontsize=10)
    buffer = BytesIO()
    canvas.print_png(buffer)
    fig.clear()
    # Indexed color keeps the full 1540x990 resolution while substantially
    # reducing SMTP upload volume for these mostly flat-color financial plots.
    from PIL import Image
    image = Image.open(buffer).convert("RGB")
    compact = BytesIO()
    image.quantize(colors=128, method=Image.Quantize.MEDIANCUT,
                   dither=Image.Dither.NONE).save(compact, format="PNG", optimize=True)
    return compact.getvalue()
