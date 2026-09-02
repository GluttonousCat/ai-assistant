# -*- encoding: utf-8 -*-
"""
蓄势启动识别器 全市场扫描 + 命中率回测

扫描: 对全 universe 在指定交易日跑 base_setup_score, 输出 DIGESTING/LAUNCHING 候选名单。
回测: 候选日后 N 日 (5/10/20) 的最大涨幅 (MFE) 与 收盘涨幅, 统计命中率与期望。

用法:
    python -m range_trading.scanner.setup_scan --date 2026-07-13 --top 20     # 单日扫描
    python -m range_trading.scanner.setup_scan --backtest --anchors 6          # 多锚点命中率回测
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from core.logger import get_logger
from range_trading.data.loader import load_daily_bars, get_latest_trade_date, load_universe
from range_trading.features.base_setup import (
    base_setup_score, is_candidate, DIGESTING, LAUNCHING,
)
from storage.pg import PgClient

logger = get_logger("range_trading.setup_scan")
OUT_DIR = Path("output/setup_scan")


def scan_setups(pg, scan_date: date, min_score: float = 55.0,
                max_score: float = 80.0,
                universe: Optional[List[str]] = None,
                lookback_days: int = 300,
                include_momentum: bool = False) -> pd.DataFrame:
    """单日全市场蓄势候选扫描 (base_setup DIGESTING + 可选 momentum_spear)
    include_momentum=True 时对每只股票同时跑动量矛, 输出动量字段 (供趋势看板双识别器)。
    分批拉取 + 每批独立连接, 避免长时间扫描中 PG 空闲断线。"""
    from range_trading.features.momentum_spear import (
        momentum_features, is_momentum_candidate, industry_weight)
    if universe is None:
        universe = load_universe(pg, scan_date, 120, 10000.0, 60)
    start = scan_date - timedelta(days=int(lookback_days * 1.6))
    records = []
    # 行业映射 (供动量行业权重)
    ind_map = {}
    if include_momentum:
        try:
            with PgClient() as pgt:
                r = pgt.fetch_all('''SELECT si.con_code, l1.industry_name AS l1 FROM stock.stock_industry si
                    JOIN stock.index_classify l2 ON si.index_code=l2.index_code AND l2.level='L2'
                    LEFT JOIN stock.index_classify l1 ON l2.parent_code=l1.industry_code AND l1.level='L1'
                    WHERE si.is_new='Y' ''')
            ind_map = {x['con_code']: x['l1'] for x in r}
        except Exception as e:
            logger.warning(f"行业映射加载失败: {e}")
    # 分批拉取: 每批临时连接 (拉完即还, 处理在内存)
    for i in range(0, len(universe), 150):
        batch = universe[i:i + 150]
        try:
            with PgClient() as pgt:
                df_all = load_daily_bars(pgt, batch, start, scan_date)
        except Exception as e:
            logger.warning(f"批次 {i // 150} 拉取失败跳过: {e}")
            continue
        if df_all.empty:
            continue
        for ts_code, g in df_all.groupby("ts_code", sort=False):
            g = g.sort_values("trade_date").reset_index(drop=True)
            if len(g) < 130 or g["trade_date"].iloc[-1] != pd.Timestamp(scan_date):
                continue
            try:
                setup = base_setup_score(g["close"], g["high"], g["low"], g["vol"])
            except Exception:
                continue
            last = setup.iloc[-1]
            base_ok = bool(is_candidate(setup, min_score, max_score).iloc[-1])
            # 动量矛 (可选)
            m_ok = False
            m_fields = {}
            if include_momentum:
                try:
                    m = momentum_features(g["close"], g["high"], g["low"], g["vol"])
                    # 注入 l1 行业列, 供 is_momentum_candidate 的 tech_only 硬过滤
                    m["l1"] = ind_map.get(ts_code, "")
                    ml = m.iloc[-1]
                    m_ok = bool(is_momentum_candidate(m, tech_only=True).iloc[-1])
                    m_fields = {
                        "momentum_state": ml["momentum_state"],
                        "momentum_score": round(float(ml["momentum_score"]), 1),
                        "m_weighted_score": round(float(ml["momentum_score"]) * industry_weight(ind_map.get(ts_code, "未知")), 1),
                        "m_dd7": round(float(ml["dd7"]), 4),
                        "m_vol_ratio": round(float(ml["vol_ratio"]), 2),
                        "m_rally20": round(float(ml["rally20"]), 4),
                        "m_limit5": int(ml["limit5"]),
                        "m_absurd": bool(ml["is_absurd"]),
                        "m_industry": ind_map.get(ts_code, ""),
                    }
                except Exception:
                    pass
            if not (base_ok or m_ok):
                continue
            records.append({
                "symbol": ts_code,
                "state": last["setup_state"],
                "detector": "momentum" if (m_ok and not base_ok) else ("both" if (m_ok and base_ok) else "base"),
                "base_score": round(float(last["base_score"]), 1),
                "score_a": round(float(last["score_a"]), 1),
                "score_b": round(float(last["score_b"]), 1),
                "score_c": round(float(last["score_c"]), 1),
                "cum_rally": round(float(last["cum_rally"]), 4),
                "rally_vol": round(float(last["rally_vol"]), 2),
                "vol_shrink": round(float(last["vol_shrink"]), 2),
                "range_pct": round(float(last["range_pct"]), 4),
                **m_fields,
                "close": float(g["close"].iloc[-1]),
            })
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records).sort_values(
        "base_score", ascending=False).reset_index(drop=True)


def _enrich_names(pg, records: pd.DataFrame) -> pd.DataFrame:
    if records.empty:
        return records
    codes = records["symbol"].tolist()
    names = {r["ts_code"]: (r.get("name") or "")
             for r in pg.fetch_all("SELECT ts_code, name FROM stock.stock_basic WHERE ts_code = ANY(%s)", (codes,))}
    records = records.copy()
    records["name"] = records["symbol"].map(names).fillna("")
    return records


def backtest_anchors(pg, anchor_dates: List[date], horizon: int = 20,
                     min_score: float = 55.0, max_score: float = 80.0) -> pd.DataFrame:
    """
    多锚点命中率回测: 每个锚点日扫描候选, 统计候选日后 horizon 日的表现。
    返回每笔候选的明细 (含 MFE/收盘涨幅/是否主升)。
    """
    rows = []
    for ad in sorted(anchor_dates):
        cands = scan_setups(pg, ad, min_score, max_score)
        if cands.empty:
            continue
        # 拉候选股后续行情
        codes = cands["symbol"].tolist()
        df_fwd = load_daily_bars(pg, codes, ad, ad + timedelta(days=int(horizon * 2) + 10))
        for _, c in cands.iterrows():
            g = df_fwd[df_fwd["ts_code"] == c["symbol"]].sort_values("trade_date").reset_index(drop=True)
            if g.empty or g["trade_date"].iloc[0] != pd.Timestamp(ad):
                continue
            entry = g["close"].iloc[0]
            fwd = g.iloc[1: horizon + 1]
            if fwd.empty:
                continue
            mfe = fwd["high"].max() / entry - 1.0
            close_ret = fwd["close"].iloc[-1] / entry - 1.0
            mae = fwd["low"].min() / entry - 1.0
            # 主升判定: horizon 内最大涨幅 >= 8% 且 最大回撤 <= 4% (强势启动)
            surge = bool(mfe >= 0.08 and mae >= -0.04)
            rows.append({
                "date": ad, "symbol": c["symbol"], "state": c["state"],
                "base_score": c["base_score"], "entry": entry,
                "mfe": round(mfe, 4), "mae": round(mae, 4),
                "close_ret": round(close_ret, 4), "surge": surge,
            })
    return pd.DataFrame(rows)


def backtest_anchors_cached(anchor_dates: List[date], horizon: int = 20,
                            min_score: float = 55.0, max_score: float = 80.0) -> pd.DataFrame:
    """带断点续跑的多锚点回测: 每个锚点独立 CSV 落盘, 已完成则跳过 (防中断丢数据)。"""
    frames = []
    for ad in sorted(anchor_dates):
        pf = OUT_DIR / f"setup_bt_{ad.strftime('%Y%m%d')}.csv"
        if pf.exists():
            logger.info(f"{ad} 回测已完成, 跳过 (断点续跑)")
            frames.append(pd.read_csv(pf))
            continue
        with PgClient() as pg:
            g = backtest_anchors(pg, [ad], horizon, min_score, max_score)
        if not g.empty:
            g.to_csv(pf, index=False, encoding="utf-8-sig")
            frames.append(g)
        logger.info(f"{ad} 回测完成: {len(g)} 笔 (已增量写盘)")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def backtest_summary_by_anchor(bt: pd.DataFrame) -> pd.DataFrame:
    """按锚点 (日期) 汇总: 每期候选数/主升率/胜率/平均MFE/平均收盘涨幅"""
    if bt.empty:
        return pd.DataFrame()
    bt = bt.copy()
    bt["date"] = pd.to_datetime(bt["date"])
    rows = []
    for d, g in bt.groupby(bt["date"].dt.date):
        rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "n": int(len(g)),
            "surge_rate": round(g["surge"].mean(), 3),
            "win_rate": round((g["close_ret"] > 0).mean(), 3),
            "avg_mfe": round(g["mfe"].mean(), 4),
            "avg_close_ret": round(g["close_ret"].mean(), 4),
            "avg_mae": round(g["mae"].mean(), 4),
        })
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description="蓄势启动识别器 扫描/回测")
    parser.add_argument("--date", type=str, default=None, help="单日扫描 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=55.0)
    parser.add_argument("--max-score", type=float, default=80.0, help="分数上限 (过滤过热票)")
    parser.add_argument("--backtest", action="store_true", help="命中率回测模式")
    parser.add_argument("--anchors", type=int, default=6)
    parser.add_argument("--horizon", type=int, default=20)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with PgClient() as pg:
        if args.backtest:
            # 2026 年 1 月起按月锚点 (每期月初, 覆盖全年)
            anchor_dates = [date(2026, m, 5) for m in range(1, 13)]
            if args.anchors:
                anchor_dates = anchor_dates[: args.anchors]
            # 只保留数据库覆盖的 (最后锚点不晚于最新交易日)
            with PgClient() as pg2:
                latest = get_latest_trade_date(pg2)
            anchor_dates = [d for d in anchor_dates if d <= latest]
            logger.info(f"2026 历史回测锚点 ({len(anchor_dates)} 期): {[d.isoformat() for d in anchor_dates]}")
            bt = backtest_anchors_cached(anchor_dates, args.horizon, args.min_score, args.max_score)
            if bt.empty:
                print("无候选产生")
                return
            bt.to_csv(OUT_DIR / "setup_backtest_2026.csv", index=False, encoding="utf-8-sig")
            by_anchor = backtest_summary_by_anchor(bt)
            by_anchor.to_csv(OUT_DIR / "setup_backtest_2026_byanchor.csv", index=False, encoding="utf-8-sig")
            _print_backtest_by_anchor(by_anchor, bt)
        else:
            scan_date = (pd.Timestamp(args.date).date() if args.date else get_latest_trade_date(pg))
            logger.info(f"蓄势扫描 {scan_date}")
            cands = scan_setups(pg, scan_date, args.min_score, args.max_score)
            if cands.empty:
                print(f"{scan_date} 无蓄势候选")
                return
            cands = _enrich_names(pg, cands)
            tag = scan_date.strftime("%Y%m%d")
            cands.to_csv(OUT_DIR / f"setup_scan_{tag}.csv", index=False, encoding="utf-8-sig")
            _print_scan(cands, args.top, scan_date)


def _print_scan(cands: pd.DataFrame, top: int, scan_date) -> None:
    print("=" * 78)
    print(f"        SURGE SETUP SCANNER   {scan_date}")
    print("=" * 78)
    state_cnt = cands["state"].value_counts().to_dict()
    print(f"候选 {len(cands)} 只 | 状态分布: {state_cnt}")
    print("-" * 78)
    for i, r in enumerate(cands.head(top).to_dict(orient="records"), 1):
        print(f"#{i:<3d}{r['symbol']} {r.get('name','')}  [{r['state']}]")
        print(f"     蓄势分: {r['base_score']:.0f}  (承接{r['score_a']:.0f}/消化{r['score_b']:.0f}/结构{r['score_c']:.0f})")
        print(f"     反弹: {r['cum_rally']*100:.0f}%  承接量比: {r['rally_vol']:.1f}  缩量: {r['vol_shrink']:.2f}  振幅: {r['range_pct']*100:.1f}%")
        print(f"     Close: {r['close']:.2f}")
        print("-" * 78)


def _print_backtest_summary(bt: pd.DataFrame, horizon: int) -> None:
    print("=" * 78)
    print(f"        蓄势识别器 命中率回测 (horizon={horizon}日)")
    print("=" * 78)
    n = len(bt)
    def agg(g):
        return pd.Series({
            "n": len(g),
            "主升率(surge)": round(g["surge"].mean(), 3),
            "胜率(ret>0)": round((g["close_ret"] > 0).mean(), 3),
            "平均MFE": round(g["mfe"].mean(), 4),
            "平均收盘涨幅": round(g["close_ret"].mean(), 4),
            "MFE>8%占比": round((g["mfe"] > 0.08).mean(), 3),
        })
    print("【整体】")
    print(agg(bt).to_string())
    print("\n【按状态】")
    print(bt.groupby("state").apply(agg, include_groups=False).to_string())
    print("\n【按分数档】")
    bt = bt.copy()
    bt["score_bin"] = pd.cut(bt["base_score"], [0, 60, 70, 80, 100], labels=["<60", "60-70", "70-80", ">=80"], right=False)
    print(bt.groupby("score_bin", observed=True).apply(agg, include_groups=False).to_string())


def _print_backtest_by_anchor(by_anchor: pd.DataFrame, bt: pd.DataFrame) -> None:
    """按锚点展示每期表现 + 累计汇总 (供前端历史回测页数据)"""
    print("=" * 78)
    print("        蓄势识别器 2026 历史回测 (按月锚点)")
    print("=" * 78)
    show = by_anchor.copy()
    show["主升率"] = (show["surge_rate"] * 100).round(0).astype(str) + "%"
    show["胜率"] = (show["win_rate"] * 100).round(0).astype(str) + "%"
    show["平均MFE"] = (show["avg_mfe"] * 100).round(1).astype(str) + "%"
    show["平均涨幅"] = (show["avg_close_ret"] * 100).round(1).astype(str) + "%"
    show["平均回撤"] = (show["avg_mae"] * 100).round(1).astype(str) + "%"
    print(show[["date", "n", "主升率", "胜率", "平均MFE", "平均涨幅", "平均回撤"]].to_string(index=False))
    print("-" * 78)
    print(f"累计: {len(bt)} 笔候选 | 总主升率 {(bt['surge'].mean()*100):.1f}% | "
          f"总胜率 {((bt['close_ret']>0).mean()*100):.1f}% | 平均MFE {(bt['mfe'].mean()*100):.1f}% | "
          f"平均收盘涨幅 {(bt['close_ret'].mean()*100):.1f}%")
    print(f"明细: {OUT_DIR/'setup_backtest_2026.csv'} | 按期汇总: {OUT_DIR/'setup_backtest_2026_byanchor.csv'}")


if __name__ == "__main__":
    main()
