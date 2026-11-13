"""Standalone research artifacts; no database or HTTP dependencies."""

import csv
import json
from pathlib import Path


def write_result(folder, result):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{result['timeframe']}_{result['strategy_id']}_{result['universe_mode']}"
    (folder / f"{name}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    for key in ["trades", "equity", "open_positions"]:
        rows = result[key]
        if rows:
            with (folder / f"{name}_{key}.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as out:
                writer = csv.DictWriter(out, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)


def write_summary(folder, results):
    from skills.quant.registry import STRATEGIES
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    folder = Path(folder)
    context = results[0]
    config = context["config"]

    rows = [
        {
            "strategy": r["strategy_id"],
            "timeframe": r["timeframe"],
            "universe_mode": r["universe_mode"],
            **r["metrics"],
        }
        for r in results
    ]
    with (folder / "summary.csv").open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# 趋势策略年度研究回测",
        "",
        f"数据区间：{results[0]['start']}—{results[0]['end']}。",
        "",
        "**固定名单全年回放存在成分前视/幸存者偏差，且策略已参考2026样例，属于样本内研究，不能视为可实现的无偏收益。**",
        "",
        f"资金{config['initial_cash']:,.0f}元、最多{config['max_positions']}只、单只目标{config['position_weight'] * 100:g}%；下根开盘、T+1、真实涨跌停、双边佣金及过户费、卖方印花税、双边滑点。",
        f"退出为固定结构止损、{config['take_profit_r']:g}R止盈、均线失守或{config['max_holding_bars']}根超时；这些退出参数预先固定，未根据本次收益调参。",
        "复权收益视作因子变化时份额同比调整的总回报近似，不是现金分红/送配股的逐笔会计台账。未平仓按末次已知收盘估值，不伪造期末卖出。",
        "",
        "| 周期 | 策略 | 名单口径 | 总收益 | 最大回撤 | 平仓笔数 | 胜率 | 盈利因子 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]

    def fmt(x):
        return "—" if x is None else f"{0.0 if x == 0 else x:.2f}"

    def pct(x):
        return "—" if x is None else f"{fmt(x)}%"

    for r in results:
        m = r["metrics"]
        lines.append(
            f"|{r['timeframe']}|{STRATEGIES[r['strategy_id']]['name']}|{r['universe_mode']}|{pct(m['total_return_pct'])}|{pct(m['max_drawdown_pct'])}|{m['closed_trades']}|{pct(m['win_rate_pct'])}|{fmt(m['profit_factor'])}|"
        )
    lines += [
        "",
        f"固定名单权重快照：{context['diagnostics']['membership_snapshot']}；实际取得：{context['diagnostics']['first_observed_at']}。observed_only只允许实际取得名单以后出现的信号，零交易不能作为有效业绩样本。",
        "数据仅从2026年开始，年初20—63个交易日处于指标预热。当前简称过滤无法恢复历史ST/退市状态。限价附近无盘口队列，模型采取保守拒单/延后成交。",
        "成交量约束以决策时已知的前一根成交量1%控制入场规模；退出未建模拆单冲击，结果不是逐笔成交仿真。",
        "基准为中证1000与沪深300价格指数，未含指数分红，与个股复权收益存在口径差异。",
        "",
    ]
    lines += ["## 固定参数下的结果解读", ""]
    for r in results:
        if (
            r["strategy_id"] != "trend_dual_v4"
            or r["universe_mode"] != "fixed_snapshot"
        ):
            continue
        metrics = r["metrics"]
        exits = sum(t["reason"] == "trend_break" for t in r["trades"])
        closed = metrics["closed_trades"]
        lines.append(
            f"- {r['timeframe']}双机会：净收益{metrics['total_return_pct']:.2f}%，最大回撤{metrics['max_drawdown_pct']:.2f}%，{closed}笔平仓中{exits}笔因均线失守退出（{exits / closed * 100 if closed else 0:.1f}%）；费用{metrics['total_fees']:,.2f}元。"
        )
    lines += [
        "",
        "这一组入场与默认退出组合没有显示出可用优势。高比例均线退出和交易成本是观察到的现象，不能仅凭这些计数认定亏损全由退出规则造成。",
        "下一步应把入场后的价格分布、退出规则和仓位分别检验，并使用新的样本验证；本轮没有为了改善结果而修改阈值或筛掉亏损交易。",
        "",
    ]
    for timeframe in sorted({r["timeframe"] for r in results}):
        chosen = [
            r
            for r in results
            if r["timeframe"] == timeframe and r["universe_mode"] == "fixed_snapshot"
        ]
        if not chosen:
            continue
        fig, ax = plt.subplots(figsize=(11, 5))
        for r in chosen:
            ax.plot(
                [p["date"] for p in r["equity"]],
                [p["equity"] / r["config"]["initial_cash"] for p in r["equity"]],
                label=r["strategy_id"],
            )
        for code in ["000852.SH", "000300.SH"]:
            points = [
                (p["date"], p.get("benchmarks", {}).get(code))
                for p in chosen[0]["equity"]
            ]
            points = [p for p in points if p[1] is not None]
            if points:
                ax.plot(
                    [p[0] for p in points],
                    [p[1] for p in points],
                    label=code,
                    alpha=0.55,
                    linestyle="--",
                )
        ticks = list(
            range(0, len(chosen[0]["equity"]), max(1, len(chosen[0]["equity"]) // 8))
        )
        ax.set_xticks(ticks)
        ax.tick_params(axis="x", rotation=25)
        ax.set_title(
            f"{timeframe}: fixed-snapshot IN-SAMPLE research (biased universe)"
        )
        ax.set_ylabel("NAV / initial capital")
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(folder / f"equity_{timeframe}.png", dpi=150)
        plt.close(fig)
        lines.append(f"![{timeframe}净值](equity_{timeframe}.png)")
    (folder / "README.md").write_text("\n".join(lines), encoding="utf-8")
