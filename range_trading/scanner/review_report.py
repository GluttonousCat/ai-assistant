"""
高质量区间标的审核报告: 三类分组出图 + TOP10 确定性排序 + 入场/止盈/止损

三类分组 (互斥):
- lower  下沿候选: RangePos < 0.30 (做多观察区)
- upper  上沿候选: RangePos > 0.75 (回落观察/持有者风控区)
- risk   趋势风险复核: 其余中 TrendRisk=MEDIUM (可能是趋势回调假阳性)

交易计划 (指南第四十二、四十三节, TP1=Mid / TP2=Upper, 止损=区间失效位):
- 下沿/中部做多: 入场 = 现价或下沿上方回踩位; 止损 = Lower - 0.5*ATR; TP1 = Mid, TP2 = Upper
- 上沿类 (回落至中轨再评估): 入场 = Mid; 止损 = Lower + 0.15*宽 (区间下半失效); TP = Upper*0.99

用法:
    python -m range_trading.scanner.review_report --date 2026-08-17 --top 10
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Rectangle

from core.logger import get_logger
from range_trading.data.loader import load_daily_bars
from storage.pg import PgClient

logger = get_logger("range_trading.review")

# 中文字体
for _f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
    try:
        font_manager.fontManager.addfont(_f)
    except Exception:
        pass
plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

RANGE_STATES = ["RANGE_FORMATION", "EARLY_TRADABLE_RANGE", "MATURE_RANGE", "RANGE_RENEWAL"]
STATE_COLOR = {
    "RANGE_FORMATION": "#ff8c00",
    "EARLY_TRADABLE_RANGE": "#2ca02c",
    "MATURE_RANGE": "#1f77b4",
    "RANGE_RENEWAL": "#9467bd",
}
STATE_BONUS = {"MATURE_RANGE": 100.0, "EARLY_TRADABLE_RANGE": 85.0,
               "RANGE_RENEWAL": 70.0, "RANGE_FORMATION": 60.0}
RISK_BONUS = {"LOW": 100.0, "MEDIUM": 50.0, "HIGH": 0.0, "NA": 50.0}


def load_industry(pg) -> pd.DataFrame:
    rows = pg.fetch_all(
        """
        SELECT si.con_code AS symbol, l2.industry_name AS ind_l2, l1.industry_name AS ind_l1
        FROM stock.stock_industry si
        JOIN stock.index_classify l2 ON l2.index_code = si.index_code AND l2.level = 'L2'
        LEFT JOIN stock.index_classify l1 ON l1.industry_code = l2.parent_code AND l1.level = 'L1'
        WHERE si.is_new = 'Y' AND si.out_date IS NULL
        """)
    return pd.DataFrame(rows).drop_duplicates("symbol")


def classify(row) -> str:
    if row["range_pos"] < 0.30:
        return "lower"
    if row["range_pos"] > 0.75:
        return "upper"
    return "risk" if row["trend_risk"] == "MEDIUM" else "mid_low"


def trade_plan(rec) -> dict:
    """按区间位置给出参考入场/止损/止盈 (价格均为前复权口径)"""
    lo, up, close = rec["lower"], rec["upper"], rec["close"]
    w = up - lo
    mid = (lo + up) / 2.0
    atr = rec["natr"] / 100.0 * close
    zone = rec["zone4"]
    if zone in ("lower", "mid_low"):
        entry = min(close, lo + 0.15 * w)          # 现价或下沿上方 15% 宽回踩
        stop = lo - 0.5 * atr                       # 区间失效: 破下沿超 0.5 ATR
        tp1, tp2 = mid, up
        action = "回踩做多" if entry < close else "现价做多"
    else:  # upper: 已在上沿区, 追高风险大, 等回落中轨
        entry = mid
        stop = lo + 0.15 * w                        # 跌回区间下半即离场
        tp1, tp2 = up * 0.99, np.nan
        action = "回落至中轨再做"
    rr = (tp1 - entry) / max(entry - stop, 1e-9)
    return {"action": action, "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "rr": rr, "mid": mid}


def certainty(rec) -> float:
    resp = min(rec["support_response"], rec["resistance_response"]) / 2.0
    return (0.35 * rec["range_score"]
            + 0.20 * STATE_BONUS.get(rec["state"], 50.0)
            + 0.20 * np.clip(resp, 0, 1) * 100.0
            + 0.10 * np.clip(rec["age"] / 20.0, 0, 1) * 100.0
            + 0.15 * RISK_BONUS.get(rec["trend_risk"], 50.0))


def plot_grid(bars: pd.DataFrame, recs: pd.DataFrame, names: dict,
              title: str, out: str, plans: dict | None = None) -> None:
    n = len(recs)
    ncols = 3
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(26, 4.6 * nrows), squeeze=False)
    fig.patch.set_facecolor("#0e1117")
    for ax in axes.flatten():
        ax.set_facecolor("#0e1117")
    for i, ax in enumerate(axes.flatten()):
        if i >= n:
            ax.axis("off")
            continue
        rec = recs.iloc[i]
        code = rec["symbol"]
        sub = bars[bars["ts_code"] == code].sort_values("trade_date").tail(60).reset_index(drop=True)
        if sub.empty:
            ax.axis("off")
            continue
        up30 = sub.tail(30)["close"].quantile(0.90)
        lo30 = sub.tail(30)["close"].quantile(0.10)
        for j, r in sub.iterrows():
            col = "#d62728" if r["close"] >= r["open"] else "#2ca02c"
            ax.plot([j, j], [r["low"], r["high"]], color=col, lw=0.8, zorder=2)
            ax.add_patch(Rectangle((j - 0.3, min(r["open"], r["close"])), 0.6,
                                   abs(r["close"] - r["open"]) + 1e-6, color=col, zorder=3))
        ax.axhline(up30, color="#4aa3ff", ls="--", lw=1.1, zorder=4)
        ax.axhline(lo30, color="#4aa3ff", ls="--", lw=1.1, zorder=4)
        ax.fill_between([-1, len(sub)], lo30, up30, color="#4aa3ff", alpha=0.07, zorder=1)
        # 交易计划线
        if plans and code in plans:
            p = plans[code]
            ax.axhline(p["entry"], color="#ffd700", lw=1.4, zorder=5)
            ax.axhline(p["stop"], color="#ff4d4f", lw=1.2, ls=":", zorder=5)
            ax.axhline(p["tp1"], color="#52c41a", lw=1.2, ls=":", zorder=5)
            if not np.isnan(p.get("tp2", np.nan)):
                ax.axhline(p["tp2"], color="#52c41a", lw=0.9, ls=":", alpha=0.6, zorder=5)
            ax.text(0.2, p["entry"], f'入场 {p["entry"]:.2f}', color="#ffd700", fontsize=7.5,
                    va="bottom", transform=ax.get_yaxis_transform())
            ax.text(0.2, p["stop"], f'止损 {p["stop"]:.2f}', color="#ff4d4f", fontsize=7.5,
                    va="bottom", transform=ax.get_yaxis_transform())
            ax.text(0.2, p["tp1"], f'TP1 {p["tp1"]:.2f}', color="#52c41a", fontsize=7.5,
                    va="bottom", transform=ax.get_yaxis_transform())
        ax.scatter([len(sub) - 1], [sub["close"].iloc[-1]], color="white", s=26, zorder=6)
        st = rec["state"].replace("RANGE_", "R_").replace("TRADABLE_", "")
        ax.set_title(
            f'{code} {names.get(code, "")}  [{st}] {rec["range_score"]:.0f}分  '
            f'pos {rec["range_pos"]:.2f}  age {rec["age"]:.0f}D  {rec["trend_risk"]}',
            color="white", fontsize=10, loc="left")
        ax.set_xlim(-1, len(sub))
        idx = np.linspace(0, len(sub) - 1, 4).astype(int)
        ax.set_xticks(idx)
        ax.set_xticklabels([sub["trade_date"].iloc[j].strftime("%m-%d") for j in idx],
                           color="#999", fontsize=7)
        ax.tick_params(colors="#888", labelsize=7)
        for sp in ax.spines.values():
            sp.set_color("#333")
        ax.grid(alpha=0.15, color="#444")
    fig.suptitle(title, color="white", fontsize=13, y=0.998)
    fig.tight_layout(rect=[0, 0, 1, 0.99])
    fig.savefig(out, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"已生成 {out}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, required=True, help="扫描日期 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=10, help="确定性 TOP N")
    parser.add_argument("--score-min", type=float, default=70.0)
    args = parser.parse_args()
    tag = args.date.replace("-", "")
    out_dir = Path("output/range_review")
    out_dir.mkdir(parents=True, exist_ok=True)

    scan = pd.read_csv(f"output/range_scan/scan_{tag}.csv")
    hq = scan[scan["state"].isin(RANGE_STATES) & (scan["range_score"] >= args.score_min)].copy()

    with PgClient() as pg:
        ind = load_industry(pg)
        names = {r["ts_code"]: (r.get("name") or "")
                 for r in pg.fetch_all("SELECT ts_code, name FROM stock.stock_basic")}
        bars = load_daily_bars(pg, hq["symbol"].tolist(),
                               pd.Timestamp(args.date) - pd.Timedelta(days=130),
                               pd.Timestamp(args.date))
    hq = hq.merge(ind, on="symbol", how="left")
    hq["zone4"] = hq.apply(classify, axis=1)

    # ---- 三类分组图 (每类取分数前 9) ----
    groups = {
        "lower": "下沿候选 (Pos<0.30, 做多观察区)",
        "upper": "上沿候选 (Pos>0.75, 回落观察区)",
        "risk": "趋势风险复核 (中部 + TrendRisk=MEDIUM)",
    }
    for key, label in groups.items():
        g = hq[hq["zone4"] == key].sort_values("range_score", ascending=False).head(9)
        if g.empty:
            print(f"[{key}] 无标的, 跳过")
            continue
        plot_grid(bars, g, names,
                  f"{args.date} {label} - {len(hq[hq['zone4'] == key])} 只, 图内为分数前 {len(g)}",
                  str(out_dir / f"{tag}_{key}.png"))

    # ---- 确定性 TOP N + 交易计划 ----
    hq["certainty"] = hq.apply(certainty, axis=1)
    top = hq.sort_values("certainty", ascending=False).head(args.top).copy()
    plans = {r["symbol"]: trade_plan(r) for _, r in top.iterrows()}
    for k in ("action", "entry", "stop", "tp1", "tp2", "rr"):
        top[k] = top["symbol"].map(lambda s, k=k: plans[s][k])
    top.to_csv(f"output/range_scan/top{args.top}_{tag}.csv", index=False, encoding="utf-8-sig")
    plot_grid(bars, top, names,
              f"{args.date} 确定性 TOP{args.top} (黄=入场, 红点线=止损, 绿点线=止盈)",
              str(out_dir / f"{tag}_top{args.top}.png"), plans=plans)

    # ---- 汇总表 ----
    pd.set_option("display.width", 250, "display.max_columns", 40)
    t = top.copy()
    t["区间"] = t.apply(lambda r: f'{r["lower"]:.2f}~{r["upper"]:.2f}', axis=1)
    t["宽%"] = (t["width"] * 100).round(1)
    t["反应"] = t["support_response"].round(1).astype(str) + "/" + t["resistance_response"].round(1).astype(str)
    t["确定性"] = t["certainty"].round(1)
    t["入场"] = t["entry"].round(2)
    t["止损"] = t["stop"].round(2)
    t["TP1"] = t["tp1"].round(2)
    t["TP2"] = t["tp2"].round(2)
    t["盈亏比"] = t["rr"].round(1)
    print()
    print(f"==== {args.date} 确定性 TOP{args.top} (按 certainty 降序) ====")
    print(t[["symbol", "name" if "name" in t else "symbol", "ind_l1", "ind_l2", "state",
             "range_score", "确定性", "区间", "宽%", "age", "range_pos", "反应",
             "trend_risk", "action", "入场", "止损", "TP1", "TP2", "盈亏比"]].to_string(index=False))


if __name__ == "__main__":
    main()
