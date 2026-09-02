"""
8.17 高质量区间标的分类评审 (一次性脚本, 不进 range_trading 包)

流程:
1. 读取 scan_20260817.csv, 三类筛选: 下沿候选 / 上沿候选 / 趋势风险;
2. 按 指南 V1 Alpha 核心 (Entry = TradableRange + RangePosition + Event)
   构造确定性排序: score + 反应强度 + 波动质量 + 位置深度 - 宽度惩罚;
3. 计算止损 (区间失效) / TP1 (区间中枢) / TP2 (对侧边界) 与期望收益 EV;
4. 标注申万一/二级行业, 导出 TOP10 表格 + 分类烛线图。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许从 scripts/ 直接运行 (导入项目包)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib import font_manager

for f in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"]:
    try:
        font_manager.fontManager.addfont(f)
    except Exception:
        pass
plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

from storage.pg import PgClient
from range_trading.data.loader import load_daily_bars

SCAN = "output/range_scan/scan_20260817.csv"
OUT_DIR = Path("output/range_review")
RANGE = ["RANGE_FORMATION", "EARLY_TRADABLE_RANGE", "MATURE_RANGE", "RANGE_RENEWAL"]
S_COLOR = {"RANGE_FORMATION": "#ff8c00", "EARLY_TRADABLE_RANGE": "#2ca02c",
           "MATURE_RANGE": "#1f77b4", "RANGE_RENEWAL": "#9467bd"}
STATE_SHORT = {"RANGE_FORMATION": "FORMATION", "EARLY_TRADABLE_RANGE": "EARLY",
               "MATURE_RANGE": "MATURE", "RANGE_RENEWAL": "RENEWAL"}


def load_data():
    df = pd.read_csv(SCAN)
    hq = df[df["state"].isin(RANGE) & (df["range_score"] >= 70)].copy()
    with PgClient() as pg:
        names = {r["ts_code"]: (r.get("name") or "")
                 for r in pg.fetch_all("SELECT ts_code, name FROM stock.stock_basic")}
        ind = pg.fetch_all("""
            SELECT si.con_code, ic2.industry_name AS l2, ic1.industry_name AS l1
            FROM stock.stock_industry si
            JOIN stock.index_classify ic2 ON ic2.index_code = si.index_code
            LEFT JOIN stock.index_classify ic1
                   ON ic1.industry_code = ic2.parent_code AND ic1.level = 'L1'
            WHERE si.is_new = 'Y' AND ic2.level = 'L2'""")
        ind_map = {r["con_code"]: (r["l1"] or "-", r["l2"] or "-") for r in ind}
        bars = load_daily_bars(pg, hq["symbol"].tolist(),
                               pd.Timestamp("2026-02-01").date(),
                               pd.Timestamp("2026-08-17").date())
    hq["name"] = hq["symbol"].map(names)
    hq["industry_l1"] = hq["symbol"].map(lambda s: ind_map.get(s, ("-", "-"))[0])
    hq["industry_l2"] = hq["symbol"].map(lambda s: ind_map.get(s, ("-", "-"))[1])
    return hq, bars


def add_rank_and_levels(hq: pd.DataFrame) -> pd.DataFrame:
    """确定性排序分 + 止损/止盈/期望收益 (单位: ATR 与 价格)"""
    resp = (hq["support_response"].clip(upper=4) + hq["resistance_response"].clip(upper=4)) / 2
    # 确定性 = 分数 × 反应强度 × 波动质量, 位置越极端加分越多, 超宽区间重罚
    hq["rank_score"] = (
        0.40 * hq["range_score"]
        + 0.20 * (resp / 4 * 100)
        + 0.15 * (hq["natr_pct"] * 100)
        + 0.10 * ((hq["range_pos"] - 0.5).abs() / 0.5 * 100)  # 距中枢越远位置分越高
        - 0.15 * ((hq["width"] / 0.6).clip(upper=1) * 100)     # 宽度惩罚
    ).round(2)

    atr_pct = hq["natr"] / 100.0
    mid = (hq["upper"] + hq["lower"]) / 2
    # 多头 (下沿): 止损 = 下沿 - 1 ATR (区间失效), TP1 = 中枢, TP2 = 上沿
    hq["stop_long"] = (hq["lower"] - hq["close"] * atr_pct).round(2)
    hq["tp1_long"] = mid.round(2)
    hq["tp2_long"] = hq["upper"].round(2)
    hq["stop_long_atr"] = ((hq["close"] - hq["stop_long"]) / (hq["close"] * atr_pct)).round(2)
    hq["tp1_long_atr"] = ((hq["tp1_long"] - hq["close"]) / (hq["close"] * atr_pct)).round(2)
    hq["tp2_long_atr"] = ((hq["tp2_long"] - hq["close"]) / (hq["close"] * atr_pct)).round(2)
    hq["rr_long"] = (hq["tp1_long_atr"] / hq["stop_long_atr"]).round(2)
    hq["ev_long_atr"] = (hq["range_prob"] * hq["tp1_long_atr"]
                       - (1 - hq["range_prob"]) * hq["stop_long_atr"]).round(2)
    # 空头 (上沿): 止损 = 上沿 + 1 ATR, TP1 = mid 与 close 的较小值 (确保 TP 在 close 下方), TP2 = 下沿
    hq["stop_short"] = (hq["upper"] + hq["close"] * atr_pct).round(2)
    tp1_short_px = pd.concat([mid, hq["close"]], axis=1).min(axis=1)
    hq["tp1_short"] = tp1_short_px.round(2)
    hq["tp2_short"] = hq["lower"].round(2)
    hq["stop_short_atr"] = ((hq["stop_short"] - hq["close"]) / (hq["close"] * atr_pct)).round(2)
    hq["tp1_short_atr"] = ((hq["close"] - tp1_short_px) / (hq["close"] * atr_pct)).round(2)
    hq["tp2_short_atr"] = ((hq["close"] - hq["tp2_short"]) / (hq["close"] * atr_pct)).round(2)
    hq["rr_short"] = (hq["tp1_short_atr"] / hq["stop_short_atr"]).round(2)
    hq["ev_short_atr"] = (hq["range_prob"] * hq["tp1_short_atr"]
                        - (1 - hq["range_prob"]) * hq["stop_short_atr"]).round(2)
    return hq


def plot_panels(df: pd.DataFrame, bars: pd.DataFrame, title: str, outfile: str,
                mode: str = "long") -> None:
    """通用 3xN 烛线面板, 叠加 30D Q90/Q10 区间与 止损/TP1/TP2 线"""
    n = len(df)
    if n == 0:
        print(f"{title}: 无标的, 跳过")
        return
    ncols = 3
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(24, 4.8 * nrows))
    fig.patch.set_facecolor("#0e1117")
    axes = np.atleast_1d(axes).flatten()

    for ax_i, ax in enumerate(axes):
        ax.set_facecolor("#0e1117")
        if ax_i >= n:
            ax.axis("off")
            continue
        rec = df.iloc[ax_i]
        code = rec["symbol"]
        sub = bars[bars["ts_code"] == code].sort_values("trade_date").reset_index(drop=True)
        if sub.empty:
            ax.axis("off"); continue
        last60 = sub.tail(60).reset_index(drop=True)
        last30 = sub.tail(30)
        up30, lo30 = last30["close"].quantile(0.90), last30["close"].quantile(0.10)
        mid30 = (up30 + lo30) / 2

        for i, r in last60.iterrows():
            col = "#d62728" if r["close"] >= r["open"] else "#2ca02c"
            ax.plot([i, i], [r["low"], r["high"]], color=col, lw=0.8, zorder=2)
            ax.add_patch(Rectangle((i - 0.3, min(r["open"], r["close"])), 0.6,
                                   abs(r["close"] - r["open"]) + 1e-6, color=col, zorder=3))
        ax.fill_between([-1, len(last60)], lo30, up30, color="#4aa3ff", alpha=0.06, zorder=1)
        ax.axhline(up30, color="#4aa3ff", ls="--", lw=1.1, zorder=4)
        ax.axhline(lo30, color="#4aa3ff", ls="--", lw=1.1, zorder=4)

        if mode == "long":
            stop, tp1, tp2 = rec["stop_long"], rec["tp1_long"], rec["tp2_long"]
            ev, rr = rec["ev_long_atr"], rec["rr_long"]
        else:
            stop, tp1, tp2 = rec["stop_short"], rec["tp1_short"], rec["tp2_short"]
            ev, rr = rec["ev_short_atr"], rec["rr_short"]
        ax.axhline(stop, color="#ff5555", ls="-", lw=1.0, zorder=5)
        ax.axhline(tp1, color="#ffd700", ls="-.", lw=0.9, zorder=5)
        ax.axhline(tp2, color="#66d9ef", ls=":", lw=0.9, zorder=5)
        xr = len(last60) - 0.5
        ax.text(xr, stop, f"止损 {stop:.2f}", color="#ff5555", fontsize=7, va="center", ha="right")
        ax.text(xr, tp1, f"TP1 {tp1:.2f}", color="#ffd700", fontsize=7, va="center", ha="right")
        ax.text(xr, tp2, f"TP2 {tp2:.2f}", color="#66d9ef", fontsize=7, va="center", ha="right")
        ax.scatter([len(last60) - 1], [last60["close"].iloc[-1]], color="yellow", s=26, zorder=6)

        t = (f"{code} {rec['name']}  [{STATE_SHORT[rec['state']]}]  分{rec['range_score']:.0f}  "
             f"pos{rec['range_pos']:.2f}  {rec['industry_l1']}/{rec['industry_l2']}\n"
             f"EV {ev:+.2f}ATR  RR {rr:.1f}  区间 {lo30:.2f}~{up30:.2f}")
        ax.set_title(t, color="white", fontsize=9, loc="left")
        ax.set_xlim(-1, len(last60))
        idx = np.linspace(0, len(last60) - 1, 5).astype(int)
        ax.set_xticks(idx)
        ax.set_xticklabels([last60["trade_date"].iloc[j].strftime("%m-%d") for j in idx],
                           color="#999", fontsize=7)
        ax.tick_params(colors="#888", labelsize=7)
        for sp in ax.spines.values():
            sp.set_color("#333")
        ax.grid(alpha=0.15, color="#444")

    fig.suptitle(title, color="white", fontsize=13, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.985])
    fig.savefig(outfile, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    print("已生成", outfile, f"({n} 只)")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hq, bars = load_data()
    hq = add_rank_and_levels(hq)

    lower_c = hq[(hq["range_pos"] < 0.20) & (hq["trend_risk"] == "LOW")].copy()
    lower_c = lower_c.sort_values("rank_score", ascending=False)
    upper_c = hq[(hq["range_pos"] > 0.80) & (hq["trend_risk"] == "LOW")].copy()
    upper_c = upper_c.sort_values("rank_score", ascending=False)
    risky = hq[hq["trend_risk"] == "MEDIUM"].copy()
    risky = risky.sort_values("rank_score", ascending=False)

    print(f"下沿候选(LOW): {len(lower_c)}  上沿候选(LOW): {len(upper_c)}  趋势风险(MEDIUM): {len(risky)}")

    # 分类出图 (全部标的)
    plot_panels(lower_c, bars, "8.17 下沿候选 (TrendRisk=LOW, RangePos<0.2) — 做多观察区",
                str(OUT_DIR / "20260817_lower.png"), mode="long")
    plot_panels(upper_c, bars, "8.17 上沿候选 (TrendRisk=LOW, RangePos>0.8) — 防假突破/观望区",
                str(OUT_DIR / "20260817_upper.png"), mode="short")
    plot_panels(risky, bars, "8.17 趋势风险 (TrendRisk=MEDIUM) — 可能为趋势回调, 谨慎",
                str(OUT_DIR / "20260817_risky.png"), mode="long")

    # TOP10: 从"有交易方向且 EV>0"的候选中取确定性最高 (EV/RR/质量)
    # 多头: Pos < 0.45 且未跌破下沿
    longs = hq[(hq["range_pos"] < 0.45) & (hq["range_pos"] > -0.05)
               & (hq["ev_long_atr"] > 0) & (hq["rr_long"] >= 0.8)].copy()
    longs["dirn"] = "LONG"
    longs["ev"] = longs["ev_long_atr"]
    longs["rr"] = longs["rr_long"]
    # 空头: 0.70 < Pos < 1.10 (在上沿但未有效突破, 假突破除外)
    shorts = hq[(hq["range_pos"] > 0.70) & (hq["range_pos"] < 1.10)
                & (hq["ev_short_atr"] > 0) & (hq["rr_short"] >= 0.8)].copy()
    shorts["dirn"] = "SHORT"
    shorts["ev"] = shorts["ev_short_atr"]
    shorts["rr"] = shorts["rr_short"]
    pool = pd.concat([longs, shorts])
    # 确定性 = EV × RR × 反应强度 × (1 - 趋势风险惩罚)
    resp = (pool["support_response"].clip(upper=4) + pool["resistance_response"].clip(upper=4)) / 2
    risk_pen = (pool["trend_risk"] == "MEDIUM").astype(float) * 0.4
    pool["certainty"] = (pool["ev"].clip(lower=0) * pool["rr"].clip(lower=0.1)
                         * (1 + resp / 4) * (1 - risk_pen)).round(3)
    # 每方向至少取 2, 其余按确定性补足
    top_l = pool[pool["dirn"] == "LONG"].nlargest(2, "certainty")
    top_s = pool[pool["dirn"] == "SHORT"].nlargest(2, "certainty")
    rest = pool.drop(index=list(top_l.index) + list(top_s.index), errors="ignore")
    top10 = pd.concat([top_l, top_s, rest]).nlargest(10, "certainty")

    top10.to_csv(OUT_DIR / "20260817_top10.csv", index=False, encoding="utf-8-sig")

    # 打印 TOP10 表格 (行业字段: 一级/二级)
    def row(r):
        if r["dirn"] == "LONG":
            plan = (f"买{r['close']:.2f} 止损{r['stop_long']:.2f} "
                    f"TP1 {r['tp1_long']:.2f} TP2 {r['tp2_long']:.2f}")
            ev, rr = r["ev_long_atr"], r["rr_long"]
        elif r["dirn"] == "SHORT":
            plan = (f"空{r['close']:.2f} 止损{r['stop_short']:.2f} "
                    f"TP1 {r['tp1_short']:.2f} TP2 {r['tp2_short']:.2f}")
            ev, rr = r["ev_short_atr"], r["rr_short"]
        else:  # WATCH: TrendRisk=MEDIUM 的多头, 需复核趋势回调风险
            plan = (f"[观察] 买{r['close']:.2f} 止损{r['stop_long']:.2f} "
                    f"TP1 {r['tp1_long']:.2f} TP2 {r['tp2_long']:.2f}")
            ev, rr = r["ev_long_atr"], r["rr_long"]
        return (r["symbol"], r["name"], f"{r['industry_l1']}/{r['industry_l2']}",
                STATE_SHORT[r["state"]], f"{r['range_score']:.0f}",
                f"{r['lower']:.2f}~{r['upper']:.2f}", f"{r['width']*100:.1f}%",
                f"{r['age']:.0f}D", f"{r['range_pos']:.2f}",
                f"{r['support_response']:.1f}/{r['resistance_response']:.1f}",
                r["trend_risk"], r["dirn"], plan,
                f"{ev:+.2f}", f"{rr:.1f}")

    cols = ["代码", "名称", "行业(一/二)", "状态", "分数", "区间", "宽度", "Age", "Pos",
            "反应S/R", "风险", "方向", "交易计划(止损/止盈)", "EV(ATR)", "RR"]
    rows = [row(r) for _, r in top10.iterrows()]
    df_show = pd.DataFrame(rows, columns=cols)
    pd.set_option("display.width", 300, "display.max_colwidth", 60)
    print()
    print("=" * 220)
    print("8.17 TOP10 高质量区间标的 (含止损/止盈, 行业标注)")
    print("=" * 220)
    print(df_show.to_string(index=False))

    # 单独出 TOP10 图
    mode = "long" if (top10["dirn"] != "SHORT").sum() >= (top10["dirn"] == "SHORT").sum() else "short"
    # 逐只按其方向出图: 简化处理, 全部按各自方向 (在 plot 内读取 dirn)
    n = len(top10)
    ncols = 2
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(22, 4.6 * nrows))
    fig.patch.set_facecolor("#0e1117")
    axes = np.atleast_1d(axes).flatten()
    for ax_i, ax in enumerate(axes):
        ax.set_facecolor("#0e1117")
        if ax_i >= n:
            ax.axis("off"); continue
        rec = top10.iloc[ax_i]
        code = rec["symbol"]
        sub = bars[bars["ts_code"] == code].sort_values("trade_date").reset_index(drop=True)
        last60 = sub.tail(60).reset_index(drop=True)
        last30 = sub.tail(30)
        up30, lo30 = last30["close"].quantile(0.90), last30["close"].quantile(0.10)
        for i, r in last60.iterrows():
            col = "#d62728" if r["close"] >= r["open"] else "#2ca02c"
            ax.plot([i, i], [r["low"], r["high"]], color=col, lw=0.8, zorder=2)
            ax.add_patch(Rectangle((i - 0.3, min(r["open"], r["close"])), 0.6,
                                   abs(r["close"] - r["open"]) + 1e-6, color=col, zorder=3))
        ax.fill_between([-1, len(last60)], lo30, up30, color="#4aa3ff", alpha=0.06, zorder=1)
        ax.axhline(up30, color="#4aa3ff", ls="--", lw=1.1, zorder=4)
        ax.axhline(lo30, color="#4aa3ff", ls="--", lw=1.1, zorder=4)
        if rec["dirn"] == "SHORT":
            stop, tp1, tp2 = rec["stop_short"], rec["tp1_short"], rec["tp2_short"]
        else:
            stop, tp1, tp2 = rec["stop_long"], rec["tp1_long"], rec["tp2_long"]
        ax.axhline(stop, color="#ff5555", lw=1.0, zorder=5)
        ax.axhline(tp1, color="#ffd700", ls="-.", lw=0.9, zorder=5)
        ax.axhline(tp2, color="#66d9ef", ls=":", lw=0.9, zorder=5)
        xr = len(last60) - 0.5
        ax.text(xr, stop, f"止损 {stop:.2f}", color="#ff5555", fontsize=8, va="center", ha="right")
        ax.text(xr, tp1, f"TP1 {tp1:.2f}", color="#ffd700", fontsize=8, va="center", ha="right")
        ax.text(xr, tp2, f"TP2 {tp2:.2f}", color="#66d9ef", fontsize=8, va="center", ha="right")
        ax.scatter([len(last60) - 1], [last60["close"].iloc[-1]], color="yellow", s=30, zorder=6)
        dirn_tag = {"LONG": "多", "SHORT": "空", "WATCH": "观察"}[rec["dirn"]]
        t = (f"#{ax_i+1} {code} {rec['name']} [{STATE_SHORT[rec['state']]}] "
             f"分{rec['range_score']:.0f} pos{rec['range_pos']:.2f} {dirn_tag}  "
             f"{rec['industry_l1']}/{rec['industry_l2']}")
        ax.set_title(t, color="white", fontsize=9.5, loc="left")
        ax.set_xlim(-1, len(last60))
        idx = np.linspace(0, len(last60) - 1, 5).astype(int)
        ax.set_xticks(idx)
        ax.set_xticklabels([last60["trade_date"].iloc[j].strftime("%m-%d") for j in idx],
                           color="#999", fontsize=7)
        ax.tick_params(colors="#888", labelsize=7)
        for sp in ax.spines.values():
            sp.set_color("#333")
        ax.grid(alpha=0.15, color="#444")
    fig.suptitle("8.17 TOP10 高质量区间标的 — 含止损/TP1/TP2", color="white", fontsize=13, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.985])
    out = str(OUT_DIR / "20260817_top10.png")
    fig.savefig(out, dpi=115, facecolor=fig.get_facecolor())
    plt.close(fig)
    print("已生成", out)


if __name__ == "__main__":
    main()
