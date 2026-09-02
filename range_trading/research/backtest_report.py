# -*- encoding: utf-8 -*-
"""
策略回测结果可视化: 读取 backtest summary/trades, 生成对比图表
输出: output/backtest/backtest_report.png
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

# 中文字体
for f in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"]:
    try:
        font_manager.fontManager.addfont(f)
    except Exception:
        pass
plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

OUT = Path("output/backtest")
BG = "#0e1117"
PANEL = "#161b22"
ACCENT = "#4aa3ff"
GOLD = "#f5b942"
UP = "#f43f5e"
DOWN = "#10b981"


def _style(ax, title):
    ax.set_facecolor(PANEL)
    ax.set_title(title, color="white", fontsize=13, pad=10, loc="left")
    ax.tick_params(colors="#8b949e", labelsize=9)
    for sp in ax.spines.values():
        sp.set_color("#2a3140")
    ax.grid(alpha=0.15, color="#444")


def main():
    summary = pd.read_csv(OUT / "summary.csv")
    trades = pd.read_csv(OUT / "trades.csv")
    if summary.empty:
        print("无回测结果")
        return

    # 策略顺序: 按期望收益排序
    summary = summary.sort_values("expectancy", ascending=False).reset_index(drop=True)
    labels = summary["strategy"].tolist()
    colors = [GOLD if "S3" in s or "S4" in s or "S5" in s else ACCENT for s in labels]
    colors = [UP if "趋势" in s else c for c, s in zip(colors, labels)]

    fig = plt.figure(figsize=(20, 13))
    fig.patch.set_facecolor(BG)
    gs = fig.add_gridspec(3, 3, hspace=0.4, wspace=0.3,
                          left=0.06, right=0.97, top=0.92, bottom=0.06)

    fig.suptitle("策略收益回测对比 (2024末~2026, 8锚点全市场, 事件驱动无前视)",
                 color="white", fontsize=16, y=0.97)

    x = np.arange(len(labels))

    # 1. 期望收益 (核心)
    ax1 = fig.add_subplot(gs[0, :2])
    ax1.bar(x, summary["expectancy"] * 100, color=colors, alpha=0.9)
    ax1.axhline(0, color="#666", lw=1)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=18, ha="right", fontsize=9)
    for i, v in enumerate(summary["expectancy"] * 100):
        ax1.text(i, v + (0.05 if v >= 0 else -0.15), f"{v:.2f}%",
                 ha="center", color="white", fontsize=9, fontweight="bold")
    _style(ax1, "期望收益 / 笔 (扣双边手续费)")

    # 2. 胜率 vs 盈亏比 (散点)
    ax2 = fig.add_subplot(gs[0, 2])
    for i, r in summary.iterrows():
        ax2.scatter(r["win_rate"] * 100, r["profit_ratio"], s=r["n"] * 2 + 40,
                    color=colors[i], alpha=0.8, edgecolors="white", linewidths=1)
        ax2.annotate(r["strategy"].split("_")[0], (r["win_rate"] * 100, r["profit_ratio"]),
                     textcoords="offset points", xytext=(6, 4), color="white", fontsize=8)
    ax2.axvline(50, color="#555", ls="--", lw=0.8)
    ax2.axhline(1.0, color="#555", ls="--", lw=0.8)
    ax2.set_xlabel("胜率 %", color="#8b949e")
    ax2.set_ylabel("盈亏比", color="#8b949e")
    _style(ax2, "胜率 × 盈亏比 (气泡=样本数)")

    # 3. 胜率
    ax3 = fig.add_subplot(gs[1, 0])
    ax3.bar(x, summary["win_rate"] * 100, color=colors, alpha=0.85)
    ax3.axhline(50, color="#666", ls="--", lw=0.8)
    ax3.set_xticks(x)
    ax3.set_xticklabels(labels, rotation=18, ha="right", fontsize=8)
    _style(ax3, "胜率 %")

    # 4. 盈亏比
    ax4 = fig.add_subplot(gs[1, 1])
    pr = summary["profit_ratio"].fillna(0)
    ax4.bar(x, pr, color=colors, alpha=0.85)
    ax4.axhline(1.0, color="#666", ls="--", lw=0.8)
    ax4.set_xticks(x)
    ax4.set_xticklabels(labels, rotation=18, ha="right", fontsize=8)
    _style(ax4, "盈亏比 (平均盈利/平均亏损)")

    # 5. 最大回撤
    ax5 = fig.add_subplot(gs[1, 2])
    ax5.bar(x, summary["max_drawdown"] * 100, color=DOWN, alpha=0.85)
    ax5.set_xticks(x)
    ax5.set_xticklabels(labels, rotation=18, ha="right", fontsize=8)
    _style(ax5, "最大回撤 %")

    # 6. 净值曲线 (累计收益)
    ax6 = fig.add_subplot(gs[2, :2])
    for i, sname in enumerate(labels):
        g = trades[trades["strategy"] == sname].sort_values("date")
        if g.empty:
            continue
        cum = (1 + g["net_ret"]).cumprod()
        ax6.plot(range(len(cum)), (cum - 1) * 100, color=colors[i],
                 lw=1.8, label=sname, alpha=0.9)
    ax6.axhline(0, color="#666", lw=1)
    ax6.legend(facecolor=PANEL, edgecolor="#2a3140", labelcolor="white", fontsize=8, loc="best")
    _style(ax6, "累计净值曲线 (按信号时间排序, %)")

    # 7. 样本数
    ax7 = fig.add_subplot(gs[2, 2])
    ax7.bar(x, summary["n"], color=colors, alpha=0.85)
    ax7.set_xticks(x)
    ax7.set_xticklabels(labels, rotation=18, ha="right", fontsize=8)
    for i, v in enumerate(summary["n"]):
        ax7.text(i, v, str(int(v)), ha="center", color="white", fontsize=8)
    _style(ax7, "样本数 (笔)")

    out = OUT / "backtest_report.png"
    fig.savefig(out, dpi=110, facecolor=fig.get_facecolor())
    print(f"图表已生成: {out}")

    # 打印结论表
    print("\n========== 策略对比汇总 ==========")
    show = summary[["strategy", "n", "win_rate", "profit_ratio", "expectancy",
                    "max_drawdown", "tp_rate", "sl_rate"]].copy()
    show["win_rate"] = (show["win_rate"] * 100).round(1).astype(str) + "%"
    show["expectancy"] = (show["expectancy"] * 100).round(2).astype(str) + "%"
    print(show.to_string(index=False))


if __name__ == "__main__":
    main()
