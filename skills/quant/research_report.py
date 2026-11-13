"""Portable MTF research evidence, including every declared exit comparison."""

import csv
from pathlib import Path


def write_research_report(folder, report, full):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    folder = Path(folder)
    rows = []
    for window, profiles in report["comparisons"].items():
        for name, r in profiles.items():
            rows.append({"window": window, "exit_profile": name, **r["metrics"]})
    with (folder / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# 60F趋势、日K入场：2026年研究",
        "",
        f"策略：M8/M13/M21。只用1—6月选择退出方案：`{report['selected_profile']}`。7—8月与9—11月固定规则分别检验，分段组合从空仓开始，分段收益不能直接相加。",
        "",
        "固定名单有成分前视与幸存者偏差；已看过8月和11月个例，设计属于样本内研究，时间留出不等于未见样本。observed_only无交易，不能作为收益验证。",
        "",
        "| 时段 | 退出 | 净收益% | 最大回撤% | 平仓 | 胜率% | 盈利因子 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]

    def fmt(value):
        return "—" if value is None else f"{value:.2f}"

    for r in rows:
        lines.append(
            f"|{r['window']}|{r['exit_profile']}|{fmt(r['total_return_pct'])}|{fmt(r['max_drawdown_pct'])}|{r['closed_trades']}|{fmt(r['win_rate_pct'])}|{fmt(r['profit_factor'])}|"
        )
    lines += [
        "",
        "single_line_2r：单根失守M21退出、2R目标、20根超时；structure_2r：不按单根均线退出，结构止损、2R目标、40根超时；protected_3r：1.2R后成本缓冲与2.5ATR只上移保护、连续两根失守M21退出、3R目标、60根超时。三者相同日K入场、现金/仓位、0.5%初始风险预算、费用与真实限价。",
        "",
        "日K收盘确认、下一交易日开盘模型成交；T+1、限价拒单、同根双触发按止损、未平仓不强卖。跟踪止损仅下一根生效，不用本根最高价反过来卖在本根较早低点。",
        "",
        "本轮所选方案全年仍为负收益，不构成可实盘有效性的证明；胜率不是唯一目标，需同时看盈利因子、净收益、回撤和交易样本量。",
        "",
        "`declaration.json`记录事先声明的三个方案与分段；`selection.json`记录开发段选择；`comparison.json/csv`保留所有对照，未删除较差结果；各时段目录包含净值和逐笔CSV。",
    ]
    (folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    fig, ax = plt.subplots(figsize=(11, 5))
    for r in full:
        profile = r["diagnostics"]["exit_profile"]
        ax.plot(
            [p["date"] for p in r["equity"]],
            [p["equity"] / r["config"]["initial_cash"] for p in r["equity"]],
            label=profile
            + (
                " (development choice)" if profile == report["selected_profile"] else ""
            ),
        )
    for code in ["000852.SH", "000300.SH"]:
        points = [
            (p["date"], p["benchmarks"].get(code))
            for p in full[0]["equity"]
            if p["benchmarks"].get(code) is not None
        ]
        ax.plot(
            [p[0] for p in points],
            [p[1] for p in points],
            label=code,
            linestyle="--",
            alpha=0.55,
        )
    dates = [p["date"] for p in full[0]["equity"]]
    ticks = list(range(0, len(dates), max(1, len(dates) // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([dates[i] for i in ticks], rotation=25)
    ax.set_title("60F trend / daily entry: fixed-membership, in-sample 2026 research")
    ax.set_ylabel("NAV / initial capital")
    ax.grid(alpha=0.2)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(folder / "exit_comparison.png", dpi=150)
    plt.close(fig)
