"""
日K Range 扫描结果审核工具: 三类分组图 + TOP10 确定性交易计划

用法:
    python scripts/range_review.py --date 20260817
输出:
    output/range_review/{date}_lower.png   下沿候选 (RangePos < 0.2, 做多观察区)
    output/range_review/{date}_upper.png   上沿候选 (RangePos > 0.8, 观望/假突破区)
    output/range_review/{date}_risk.png    趋势风险 (TrendRisk=MEDIUM, 需人工复核)
    output/range_review/{date}_top10.png   TOP10 交易计划 (入场带/TP1/TP2/止损)
    output/range_review/{date}_top10.csv   TOP10 明细表

交易计划口径 (指南第四十二、四十三章):
    入场: 下沿带 [Lower+0.05w, Lower+0.20w]; 已在带内则现价可入, 否则挂回踩限价
    TP1 = Range Mid (第一目标), TP2 = Upper (第二目标)
    止损 = Lower - 0.5*ATR (Range Invalidity: 有效跌破下沿+缓冲, 非固定百分比)
"""
from __future__ import annotations

import argparse
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib import font_manager

from core.logger import get_logger
from range_trading.data.loader import load_daily_bars
from storage.pg import PgClient

logger = get_logger("range_review")

for _f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
    try:
        font_manager.fontManager.addfont(_f)
    except Exception:
        pass
plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

STATE_COLOR = {
    "RANGE_FORMATION": "#ff8c00", "EARLY_TRADABLE_RANGE": "#2ca02c",
    "MATURE_RANGE": "#1f77b4", "RANGE_RENEWAL": "#9467bd",
}
RANGE_STATES = list(STATE_COLOR)
BG = "#0e1117"


def load_meta(pg) -> tuple[dict, pd.DataFrame]:
    names = {r["ts_code"]: (r.get("name") or "") for r in
             pg.fetch_all("SELECT ts_code, name FROM stock.stock_basic")}
    industry = pg.fetch_df("""
        SELECT si.con_code AS ts_code,
               l2.industry_name AS ind_l2, l1.industry_name AS ind_l1
        FROM stock_industry si
        JOIN index_classify l2 ON l2.index_code = si.index_code AND l2.level = 'L2'
        LEFT JOIN index_classify l1 ON l1.industry_code = l2.parent_code AND l1.level = 'L1'
        WHERE si.is_new = 'Y'
    """)
    return names, industry.set_index("ts_code")


def zone_label(p: float) -> str:
    return "下沿" if p < 0.2 else ("上沿" if p > 0.8 else "中部")


def draw_group(ax, rec: dict, bars: pd.DataFrame, name: str,
               plan: dict | None = None) -> None:
    """单标的K线 + 30D分位区间 + (可选)交易计划线"""
    d = bars.tail(60).reset_index(drop=True)
    lo, up = rec["lower"], rec["upper"]
    mid, w = (lo + up) / 2.0, up - lo

    xs = np.arange(len(d))
    for i, r in d.iterrows():
        col = "#d62728" if r["close"] >= r["open"] else "#2ca02c"
        ax.plot([i, i], [r["low"], r["high"]], color=col, lw=0.8, zorder=2)
        ax.add_patch(Rectangle((i - 0.3, min(r["open"], r["close"])), 0.6,
                               abs(r["close"] - r["open"]) + 1e-6, color=col, zorder=3))
    ax.axhline(up, color="#4aa3ff", ls="--", lw=1.2)
    ax.axhline(lo, color="#4aa3ff", ls="--", lw=1.2)
    ax.fill_between([-1, len(d)], lo, up, color="#4aa3ff", alpha=0.07, zorder=1)
    ax.text(len(d) - 0.5, up, f"{up:.2f}", color="#4aa3ff", fontsize=7, va="bottom", ha="right")
    ax.text(len(d) - 0.5, lo, f"{lo:.2f}", color="#4aa3ff", fontsize=7, va="top", ha="right")
    ax.scatter([len(d) - 1], [d["close"].iloc[-1]], color="yellow", s=28, zorder=6)

    if plan:
        ez = plan["entry_zone"]
        ax.fill_between([-1, len(d)], ez[0], ez[1], color="#ffd700", alpha=0.22, zorder=1)
        ax.axhline(plan["tp1"], color="#2ca02c", ls=":", lw=1.2)
        ax.axhline(plan["tp2"], color="#2ca02c", ls=":", lw=1.0, alpha=0.6)
        ax.axhline(plan["sl"], color="#d62728", ls="-.", lw=1.2)
        ax.text(0.2, plan["tp1"], f"TP1 {plan['tp1']:.2f}", color="#2ca02c", fontsize=7, va="bottom")
        ax.text(0.2, plan["tp2"], f"TP2 {plan['tp2']:.2f}", color="#2ca02c", fontsize=7, va="bottom", alpha=0.8)
        ax.text(0.2, plan["sl"], f"SL {plan['sl']:.2f}", color="#d62728", fontsize=7, va="bottom")

    title = (f"{rec['symbol']} {name}  [{rec['state'][:12]}]  score {rec['range_score']:.0f}  "
             f"pos {rec['range_pos']:.2f}({zone_label(rec['range_pos'])})  age {rec['age']:.0f}D  "
             f"{rec['trend_risk']}")
    ax.set_title(title, color="white", fontsize=9.5, loc="left")
    ax.set_facecolor(BG)
    ax.set_xlim(-1, len(d))
    idx = np.linspace(0, len(d) - 1, 5).astype(int)
    ax.set_xticks(idx)
    ax.set_xticklabels([d["trade_date"].iloc[j].strftime("%m-%d") for j in idx],
                       color="#999", fontsize=7)
    ax.tick_params(colors="#888", labelsize=7)
    for sp in ax.spines.values():
        sp.set_color("#333")
    ax.grid(alpha=0.15, color="#444")


def render_pages(records: list[dict], bars_all: pd.DataFrame, names: dict,
                 out_path: str, subtitle: str, plans: dict | None = None) -> None:
    per = 9
    for fi in range(0, len(records), per):
        chunk = records[fi:fi + per]
        fig, axes = plt.subplots(3, 3, figsize=(26, 15))
        fig.patch.set_facecolor(BG)
        for k, ax in enumerate(axes.flatten()):
            if k >= len(chunk):
                ax.axis("off")
                continue
            rec = chunk[k]
            sub = bars_all[bars_all["ts_code"] == rec["symbol"]]
            draw_group(ax, rec, sub, names.get(rec["symbol"], ""),
                       plan=(plans or {}).get(rec["symbol"]))
        fig.suptitle(f"{subtitle} (第 {fi + 1}~{fi + len(chunk)} / {len(records)} 只)",
                     color="white", fontsize=13, y=0.995)
        fig.tight_layout(rect=[0, 0, 1, 0.985])
        suffix = f"_p{fi // per + 1:02d}" if len(records) > per else ""
        fig.savefig(out_path.replace(".png", f"{suffix}.png"), dpi=110, facecolor=BG)
        plt.close(fig)
        logger.info(f"出图: {out_path.replace('.png', suffix + '.png')}")


def certainty_score(r: pd.Series) -> float:
    """确定性评分 (做多视角): 分数 + 状态成熟度 + 低风险 + 贴下沿 + 反应强 + 触碰多 + 宽度适中"""
    s = r["range_score"]
    s += {"MATURE_RANGE": 8, "EARLY_TRADABLE_RANGE": 6,
          "RANGE_RENEWAL": 3, "RANGE_FORMATION": 0}.get(r["state"], 0)
    s += 8 if r["trend_risk"] == "LOW" else -5
    s += float(np.clip(1 - abs(r["range_pos"] - 0.15) / 0.4, 0, 1)) * 10
    s += float(np.clip((r["support_response"] + r["resistance_response"]) / 2 / 2.0, 0, 1)) * 6
    s += float(np.clip(r["touch"] / 15.0, 0, 1)) * 4
    s += 6 if 0.08 <= r["width"] <= 0.28 else (3 if r["width"] <= 0.35 else 0)
    s += 3 if r["age"] >= 15 else (2 if r["age"] >= 5 else -2)
    return s


def build_plan(r: pd.Series) -> dict:
    """交易计划: 入场带 / TP1=Mid / TP2=Upper / SL=Lower-0.5ATR"""
    lo, up = r["lower"], r["upper"]
    mid, w = (lo + up) / 2.0, up - lo
    atr = r["natr"] / 100.0 * r["close"]
    if r["range_pos"] <= 0.22:      # 已在下沿带内: 现价可分批入
        entry_zone = (lo + 0.05 * w, lo + 0.22 * w)
        entry_ref = max(r["close"], lo + 0.05 * w)
    else:                            # 中部偏下: 挂回踩限价, 不追
        entry_zone = (lo + 0.08 * w, lo + 0.25 * w)
        entry_ref = lo + 0.15 * w
    sl = lo - 0.5 * atr
    rr = (mid - entry_ref) / (entry_ref - sl) if entry_ref > sl else np.nan
    return {"entry_zone": entry_zone, "entry_ref": entry_ref,
            "tp1": mid, "tp2": up, "sl": sl, "rr": rr, "atr": atr}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, required=True, help="扫描日期 YYYYMMDD")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--score-min", type=float, default=70.0)
    args = parser.parse_args()
    tag = datetime.strptime(args.date, "%Y%m%d").strftime("%Y-%m-%d")

    df = pd.read_csv(f"output/range_scan/scan_{args.date}.csv")
    hq = df[df["state"].isin(RANGE_STATES) & (df["range_score"] >= args.score_min)].copy()

    with PgClient() as pg:
        names, industry = load_meta(pg)
        bars = load_daily_bars(pg, hq["symbol"].tolist(),
                               pd.Timestamp(tag).date() - pd.Timedelta(days=130),
                               pd.Timestamp(tag).date())

    hq["certainty"] = hq.apply(certainty_score, axis=1)
    recs = hq.to_dict("records")

    lower_group = [r for r in recs if r["range_pos"] < 0.2]
    upper_group = [r for r in recs if r["range_pos"] > 0.8]
    risk_group = [r for r in recs if r["trend_risk"] != "LOW"]

    render_pages(lower_group, bars, names, f"output/range_review/{args.date}_lower.png",
                 f"{tag} 下沿候选 (Pos<0.2, 做多观察区)")
    render_pages(upper_group, bars, names, f"output/range_review/{args.date}_upper.png",
                 f"{tag} 上沿候选 (Pos>0.8, 观望/防假突破)")
    render_pages(risk_group, bars, names, f"output/range_review/{args.date}_risk.png",
                 f"{tag} 趋势风险复核 (TrendRisk=MEDIUM, 可能是趋势回调)")

    # ---- TOP10: 做多视角确定性最高 (要求 pos<=0.45 保证到 Mid 有空间) ----
    top = hq[hq["range_pos"] <= 0.45].sort_values("certainty", ascending=False).head(args.top)
    plans = {}
    rows = []
    for _, r in top.iterrows():
        p = build_plan(r)
        plans[r["symbol"]] = p
        ind = industry.loc[r["symbol"]] if r["symbol"] in industry.index else None
        rows.append({
            "symbol": r["symbol"], "name": names.get(r["symbol"], ""),
            "ind_l1": ind["ind_l1"] if ind is not None else "",
            "ind_l2": ind["ind_l2"] if ind is not None else "",
            "state": r["state"], "score": r["range_score"], "certainty": r["certainty"],
            "lower": r["lower"], "upper": r["upper"], "mid": p["tp1"],
            "width%": round(r["width"] * 100, 1), "age": r["age"], "pos": r["range_pos"],
            "sup/res": f"{r['support_response']:.1f}/{r['resistance_response']:.1f}",
            "trend_risk": r["trend_risk"], "close": r["close"], "atr": p["atr"],
            "entry_zone": f"{p['entry_zone'][0]:.2f}~{p['entry_zone'][1]:.2f}",
            "entry_ref": p["entry_ref"], "TP1": p["tp1"], "TP2": p["tp2"], "SL": p["sl"],
            "RR(TP1)": round(p["rr"], 2),
        })
    top_df = pd.DataFrame(rows)
    top_df.to_csv(f"output/range_review/{args.date}_top10.csv",
                  index=False, encoding="utf-8-sig")

    top_recs = top.to_dict("records")
    render_pages(top_recs, bars, names, f"output/range_review/{args.date}_top10.png",
                 f"{tag} TOP{len(top_recs)} 确定性最高 (黄带=入场区, 绿点线=TP1/TP2, 红点划线=SL)",
                 plans=plans)

    pd.set_option("display.width", 250, "display.max_columns", 30)
    print(top_df[["symbol", "name", "ind_l1", "ind_l2", "state", "score", "certainty",
                  "lower", "upper", "width%", "age", "pos", "sup/res", "trend_risk",
                  "close", "entry_zone", "entry_ref", "TP1", "TP2", "SL", "RR(TP1)"]].to_string(index=False))


if __name__ == "__main__":
    main()
