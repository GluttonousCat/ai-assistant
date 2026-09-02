"""
历史校验主引擎

流程:
  24 个锚点 x 每锚点 ~160 只分层标的
  -> 对每只在 anchor 时点跑 run_daily_regime (全历史, 取 anchor 行快照)
  -> 输出信号明细 (state/score/特征/前瞻 5/10/20 日区间维持度)
  -> 汇总: 信号组 vs 基线(随机/非信号) 的区间维持度差异, 领先性分布, 市场状态分层

多进程并行 (每 worker 独立 PG 连接); 结果落盘 CSV + 打印报告。

用法:
    python -m range_trading.research.run_validation
    python -m range_trading.research.run_validation --per-group 40 --workers 8
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from multiprocessing import Pool
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from range_trading.config import DEFAULT_DAILY_CONFIG
from range_trading.data.loader import load_daily_bars
from range_trading.regime.daily_regime import run_daily_regime
from range_trading.regime.state_machine import RANGE_STATES
from range_trading.research.event_study import (
    find_range_anchors,
    forward_metrics,
    is_range_signal,
    lead_time,
)
from range_trading.research.sampler import (
    build_anchor_dates,
    eligible_universe,
    market_regime_snapshot,
    stratified_sample,
)
from storage.pg import PgClient

DATA_START = date(2024, 1, 24)
HORIZONS = (5, 10, 20)
MIN_SCORE = 55.0          # range_formation 阈值
LEAD_LOOKBACK = 10

# 每 worker 进程的连接 (fork/spawn 后各自持有)
_PG: Optional[PgClient] = None


def _worker_pg() -> PgClient:
    global _PG
    if _PG is None:
        _PG = PgClient()
        _PG.__enter__()
    return _PG


def _analyze_one(task: Dict) -> List[Dict]:
    """单标的跨其所有锚点: 一次取全历史, 逐锚点出快照 + 前瞻指标 + 领先性"""
    code = task["ts_code"]
    anchors: List[Dict] = task["anchors"]          # [{anchor, tag, regime_note}]
    pg = _worker_pg()
    cfg = DEFAULT_DAILY_CONFIG

    latest = max(a["anchor"] for a in anchors)
    df = load_daily_bars(pg, [code], DATA_START, latest + timedelta(days=40))
    if df.empty or len(df) < 80:
        return []
    df = df.sort_values("trade_date").reset_index(drop=True)
    detail, _ = run_daily_regime(df, symbol=code, config=cfg)
    detail["date_only"] = detail["trade_date"].dt.date

    # 真区间起点 (整段历史标定一次)
    anchors_idx = find_range_anchors(df, cfg)
    out: List[Dict] = []
    for a in anchors:
        adate = a["anchor"]
        hit = detail.index[detail["date_only"] == adate]
        if len(hit) == 0:
            continue
        idx = int(hit[0])
        row = detail.iloc[idx]
        score = float(row.get("daily_range_score", np.nan))
        state = row.get("state", "NA")
        if np.isnan(score):
            continue
        is_sig = is_range_signal(state, score, MIN_SCORE)

        rec: Dict = {
            "ts_code": code, "anchor": adate, "regime_tag": a["tag"],
            "regime_note": a["regime_note"], "state": state, "score": score,
            "is_signal": is_sig,
            "di": float(row.get("di", np.nan)), "di_slope": float(row.get("di_slope", np.nan)),
            "flip": float(row.get("flip", np.nan)), "ac1": float(row.get("ac1", np.nan)),
            "range_pos": float(row.get("range_pos", np.nan)),
            "width": float(row.get("range_width", np.nan)),
            "touch": float(row.get("touch", np.nan)),
            "natr_pct": float(row.get("natr_pct", np.nan)),
            "trend_cont": float(row.get("trend_cont", np.nan)),
        }
        # 前瞻性
        for h in HORIZONS:
            m = forward_metrics(df, detail, idx, h, cfg)
            if m:
                for k, v in m.items():
                    rec[f"{k}_{h}"] = v
        # 领先性: 该 anchor 是否恰为某真区间起点 -> 信号是否领先
        # 找 anchor 附近 (前后3天) 的真区间起点
        near = [ai for ai in anchors_idx if abs(ai - idx) <= 3]
        if near:
            lead, detected = lead_time(detail, near[0], MIN_SCORE, LEAD_LOOKBACK)
            rec["is_anchor_event"] = True
            rec["lead_days"] = lead
            rec["anchor_detected"] = detected
        else:
            rec["is_anchor_event"] = False
        out.append(rec)
    return out


def run_validation(per_group: int = 40, workers: int = 8,
                   out_dir: str = "output/range_scan") -> pd.DataFrame:
    cfg = DEFAULT_DAILY_CONFIG
    with PgClient() as pg:
        anchors = build_anchor_dates(pg)
        print(f"锚点 {len(anchors)} 个")
        # 市场状态快照
        regime_meta: Dict[str, Dict] = {}
        for a in anchors:
            regime_meta[str(a["anchor"])] = market_regime_snapshot(pg, a["anchor"])

        # 每锚点独立 universe + 抽样, 但同一只可能跨锚点复用 -> 归并任务
        code_to_anchors: Dict[str, List[Dict]] = {}
        for a in anchors:
            u = eligible_universe(pg, a["anchor"])
            samp = stratified_sample(u, per_group=per_group)
            for c in samp:
                code_to_anchors.setdefault(c, []).append(a)
        tasks = [{"ts_code": c, "anchors": al} for c, al in code_to_anchors.items()]
        total_points = sum(len(v) for v in code_to_anchors.values())
        print(f"标的 {len(tasks)} 只, 信号点 {total_points} 个 (每标的跨锚点复用一次历史)")

    records: List[Dict] = []
    with Pool(workers) as pool:
        for i, recs in enumerate(pool.imap_unordered(_analyze_one, tasks, chunksize=8)):
            records.extend(recs)
            if (i + 1) % 200 == 0:
                print(f"  进度 {i + 1}/{len(tasks)} 标的, 累计 {len(records)} 信号点")

    df = pd.DataFrame(records)
    os.makedirs(out_dir, exist_ok=True)
    df.to_csv(os.path.join(out_dir, "validation_detail.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame([{"anchor": k, **v} for k, v in regime_meta.items()]).to_csv(
        os.path.join(out_dir, "validation_regime_meta.csv"), index=False, encoding="utf-8-sig")
    print(f"明细已落盘: {out_dir}/validation_detail.csv  共 {len(df)} 行")
    return df


# ---------------------------------------------------------------- 汇总分析

def summarize(df: pd.DataFrame) -> None:
    pd.set_option("display.width", 200, "display.max_columns", 40)
    print("\n" + "=" * 78)
    print("  前瞻性检验: 信号组(RANGE族+score>=55) vs 非信号组  区间维持度对比")
    print("=" * 78)
    sig = df[df["is_signal"]]
    base = df[~df["is_signal"]]
    print(f"样本: 信号组 {len(sig)} | 非信号组 {len(base)}")
    rows = []
    for h in HORIZONS:
        for grp, name in [(sig, "信号组"), (base, "非信号")]:
            rows.append({
                "horizon": h, "group": name,
                "persist": grp[f"persist_ratio_{h}"].mean(),
                "drift_natr": grp[f"drift_natr_{h}"].mean(),
                "holding%": grp[f"range_holding_{h}"].mean() * 100,
                "no_breakout%": grp[f"no_breakout_{h}"].mean() * 100,
            })
    piv = pd.DataFrame(rows)
    for h in HORIZONS:
        s = piv[(piv.horizon == h) & (piv.group == "信号组")].iloc[0]
        b = piv[(piv.horizon == h) & (piv.group == "非信号")].iloc[0]
        print(f"  H={h:2d}  信号 persist={s.persist:.2f} drift={s.drift_natr:.2f}ATR "
              f"holding={s['holding%']:.0f}% nobreak={s['no_breakout%']:.0f}%  |  "
              f"基线 persist={b.persist:.2f} drift={b.drift_natr:.2f}ATR holding={b['holding%']:.0f}%")

    print("\n  按市场状态分层 (信号组 holding% @ H=10):")
    st = sig.groupby("regime_note")[f"range_holding_10"].agg(["mean", "count"])
    st["mean"] = (st["mean"] * 100).round(1)
    print(st.to_string())

    print("\n  按信号状态细分 (holding% @ H=10):")
    st2 = sig.groupby("state")[f"range_holding_10"].agg(["mean", "count"])
    st2["mean"] = (st2["mean"] * 100).round(1)
    print(st2.to_string())

    print("\n  领先性检验 (Event-anchored, 真区间起点附近):")
    ev = df[df.get("is_anchor_event") == True]
    if len(ev):
        det = ev["anchor_detected"].mean() * 100
        leads = ev.loc[ev["anchor_detected"] == True, "lead_days"]
        print(f"  真区间起点样本 {len(ev)} | 信号检出率 {det:.1f}% | "
              f"领先天数 mean={leads.mean():.1f} median={leads.median():.0f} "
              f"领先>=1天占比 {(leads >= 1).mean() * 100:.0f}%")
    else:
        print("  无真区间起点样本")

    # 分数-效果关系 (信号是否单调)
    print("\n  分数分桶 vs 区间维持 (H=10 holding%):")
    df2 = df.dropna(subset=[f"range_holding_10"])
    df2 = df2.copy()
    df2["bucket"] = pd.cut(df2["score"], [0, 40, 55, 70, 85, 100],
                           labels=["<40", "40-55", "55-70", "70-85", "85+"])
    bt = df2.groupby("bucket", observed=True)[f"range_holding_10"].agg(["mean", "count"])
    bt["mean"] = (bt["mean"] * 100).round(1)
    print(bt.to_string())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-group", type=int, default=40, help="每板块前缀抽样数 (x4 组)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", type=str, default="output/range_scan")
    ap.add_argument("--analyze-only", action="store_true", help="只汇总已有明细")
    args = ap.parse_args()

    detail_path = os.path.join(args.out, "validation_detail.csv")
    if args.analyze_only and os.path.exists(detail_path):
        df = pd.read_csv(detail_path)
    else:
        df = run_validation(args.per_group, args.workers, args.out)
    if not df.empty:
        summarize(df)


if __name__ == "__main__":
    main()
