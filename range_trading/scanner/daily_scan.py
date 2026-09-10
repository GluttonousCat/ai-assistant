"""
日K全市场慢扫描 (指南第三十二、三十三章第一阶段)

每天对全 universe 计算日K Regime, 输出 Ranking Table:
不是寻找"已经震荡"的股票, 而是寻找"正在形成可交易震荡"的标的。

用法:
    python -m range_trading.scanner.daily_scan --top 20
    python -m range_trading.scanner.daily_scan --date 2026-08-14 --limit 100
    python -m range_trading.scanner.daily_scan --symbol 000001.SZ
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd

from core.logger import get_logger
from range_trading.config import DEFAULT_DAILY_CONFIG
from range_trading.data.loader import (
    estimate_start_date,
    get_latest_trade_date,
    iter_symbol_frames,
    load_daily_bars,
    load_universe,
)
from range_trading.regime.daily_regime import run_daily_regime
from range_trading.regime.state_machine import RANGE_STATES
from storage.pg import PgClient

logger = get_logger("range_trading.scanner")


def scan_market(scan_date: Optional[date] = None, top: int = 20,
                min_history: Optional[int] = None,
                min_amount: Optional[float] = None,
                limit: Optional[int] = None,
                out_dir: str = "output/range_scan") -> pd.DataFrame:
    """
    全市场日K扫描主流程, 返回 Ranking DataFrame 并落盘 CSV/JSON。
    """
    cfg = DEFAULT_DAILY_CONFIG
    min_history = min_history if min_history is not None else cfg.min_history
    min_amount = min_amount if min_amount is not None else cfg.universe_amount_floor

    records: List[dict] = []
    with PgClient() as pg:
        if scan_date is None:
            scan_date = get_latest_trade_date(pg)
        if scan_date is None:
            logger.error("stock.daily 无数据, 终止扫描")
            return pd.DataFrame()
        universe = load_universe(pg, scan_date, min_history, min_amount,
                                 cfg.universe_lookback)
        if limit:
            universe = universe[:limit]
        logger.info(f"扫描 {scan_date} | universe {len(universe)} 只 "
                    f"(min_history={min_history}, min_amount={min_amount}千元)")

        start = estimate_start_date(scan_date)
        n_done = n_skipped = 0
        for ts_code, df in iter_symbol_frames(pg, universe, start, scan_date,
                                              cfg.scan_batch_size):
            n_done += 1
            last_date = df["trade_date"].iloc[-1]
            if last_date != pd.Timestamp(scan_date):
                n_skipped += 1        # 扫描日停牌/无数据
                continue
            try:
                _, state = run_daily_regime(df, symbol=ts_code, config=cfg)
            except Exception as e:    # 单只失败不阻塞全市场扫描
                logger.warning(f"{ts_code} 分析失败: {e}")
                continue
            if state is not None:
                records.append(state.to_dict())

    logger.info(f"完成: 分析 {n_done} 只, 跳过停牌 {n_skipped} 只, 有效 {len(records)} 只")

    if not records:
        logger.warning("无有效扫描结果")
        return pd.DataFrame()

    result = pd.DataFrame(records)
    # Range 优先视角: 区间族在前, 其余按分数; 同组内按分数降序
    result["_is_range"] = result["state"].isin(RANGE_STATES).astype(int)
    result = result.sort_values(
        ["_is_range", "range_score", "range_prob"],
        ascending=[False, False, False],
    ).drop(columns="_is_range").reset_index(drop=True)

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    tag = scan_date.strftime("%Y%m%d") if isinstance(scan_date, date) else str(scan_date)
    result.to_csv(out_path / f"scan_{tag}.csv", index=False, encoding="utf-8-sig")
    (out_path / f"scan_{tag}.json").write_text(
        json.dumps(result.to_dict(orient="records"), ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")

    print_ranking(result, top, scan_date)
    return result


def print_ranking(result: pd.DataFrame, top: int, scan_date) -> None:
    """控制台 Ranking Table (指南第三十二章)"""
    state_counts = result["state"].value_counts().to_dict()
    print("=" * 72)
    print(f"        DAILY RANGE SCANNER   {scan_date}")
    print("=" * 72)
    print(f"状态分布: { {k: v for k, v in sorted(state_counts.items(), key=lambda x: -x[1])} }")
    print("-" * 72)
    view = result.head(top)
    for rank, rec in enumerate(view.to_dict(orient="records"), start=1):
        print(f"#{rank:<3d}{rec['symbol']:<12s}[{rec['state']}]")
        age = f"{int(rec['age'])}D" if rec.get("age") is not None else "-"
        width_pct = rec["width"] * 100 if pd.notna(rec["width"]) else float("nan")
        print(f"     Range:     {rec['lower']:.2f} ~ {rec['upper']:.2f}"
              f"   (width {width_pct:.1f}%)   Age: {age}")
        print(f"     Score:     {rec['range_score']:.0f}   (prob {rec['range_prob']:.2f})"
              f"   DI: {rec['di']:.2f} (slope {rec['di_slope']:+.2f})"
              f"   Flip: {rec['flip_rate']:.2f}   AC1: {rec['ac1']:+.2f}")
        print(f"     Touch:     {rec['touch']:.0f}   SupResp: {rec['support_response']:.1f} ATR"
              f"   ResResp: {rec['resistance_response']:.1f} ATR"
              f"   NATR: {rec['natr']:.1f}% (pct {rec['natr_pct']:.2f})")
        print(f"     Volume:    ratio {rec['vol_ratio']:.2f}   pct {rec['vol_pct']:.2f}"
              f"   OBV slope {rec['obv_slope']:+.2f}   VWAP dev {rec['vwap_dev']:+.2f}")
        print(f"     TrendRisk: {rec['trend_risk']}   LongBias: {rec['long_bias']:+.0f}"
              f"   RangeCompat: {rec['range_compat']:.0f}")
        print(f"     Close:     {rec['close']:.2f}   (RangePos {rec['range_pos']:.2f})")
        print("-" * 72)


def describe_symbol(symbol: str, as_of: Optional[date] = None,
                    days: int = 40) -> None:
    """
    单标的日K报告 (指南第十七章 "日K最重要的输出" 格式)
    并展示最近 days 个交易日的状态演变。
    """
    cfg = DEFAULT_DAILY_CONFIG
    with PgClient() as pg:
        if as_of is None:
            as_of = get_latest_trade_date(pg)
        start = estimate_start_date(as_of)
        df = load_daily_bars(pg, [symbol], start, as_of)
    if df.empty:
        logger.warning(f"{symbol}: 无数据")
        return

    detail, state = run_daily_regime(df, symbol=symbol, config=cfg)

    print("=" * 72)
    print(f"        DAILY REGIME REPORT   {symbol}   as of {as_of}")
    print("=" * 72)
    if state is None:
        print("数据不足, 无法生成 Regime")
        return
    age = f"{int(state.age)}D" if state.age is not None else "-"
    width_pct = state.width * 100 if pd.notna(state.width) else float("nan")
    print(f"Daily Regime:   {state.state}")
    print(f"Range:          {state.lower:.2f} ~ {state.upper:.2f}  (width {width_pct:.1f}%)")
    print(f"RangeScore:     {state.range_score:.0f}   (prob {state.range_prob:.2f})   Age: {age}")
    print(f"Support:        {state.lower:.2f}   Resistance: {state.upper:.2f}")
    print(f"DI:             {state.di:.2f} (slope {state.di_slope:+.2f})"
          f"   FlipRate: {state.flip_rate:.2f}   AC1: {state.ac1:+.2f}")
    print(f"BoundaryStab:   up {state.upper_stability:.2f} / low {state.lower_stability:.2f}"
          f"   Touch: {state.touch:.0f}"
          f"   SupResp: {state.support_response:.1f}ATR / ResResp: {state.resistance_response:.1f}ATR")
    print(f"NATR:           {state.natr:.2f}% (pct {state.natr_pct:.2f})")
    print(f"Volume:         ratio {state.vol_ratio:.2f}   pct {state.vol_pct:.2f}"
          f"   OBV slope {state.obv_slope:+.2f}   VWAP dev {state.vwap_dev:+.2f}")
    print(f"TrendRisk:      {state.trend_risk}   LongBias: {state.long_bias:+.0f}"
          f"   ShortBias: {state.short_bias:+.0f}   RangeCompat: {state.range_compat:.0f}")
    print(f"Close:          {state.close:.2f}  (RangePos {state.range_pos:.2f})")
    print("-" * 72)
    print(f"最近 {days} 个交易日状态演变 (仅变化点):")
    tail = detail.dropna(subset=["daily_range_score"]).tail(days)
    prev = None
    for _, row in tail.iterrows():
        if row["state"] != prev:
            d = row["trade_date"]
            d = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else d
            print(f"  {d}  {row['state']:<24s} score={row['daily_range_score']:.0f}")
            prev = row["state"]


def main() -> None:
    parser = argparse.ArgumentParser(description="日K Range Scanner")
    parser.add_argument("--date", type=str, default=None, help="扫描日 YYYY-MM-DD (默认最新交易日)")
    parser.add_argument("--top", type=int, default=20, help="控制台显示 Top N")
    parser.add_argument("--symbol", type=str, default=None, help="单标的报告模式")
    parser.add_argument("--limit", type=int, default=None, help="限制 universe 数量 (调试)")
    parser.add_argument("--min-history", type=int, default=None, help="最少日K根数")
    parser.add_argument("--min-amount", type=float, default=None, help="日均成交额下限 (千元)")
    parser.add_argument("--out", type=str, default="output/range_scan", help="结果输出目录")
    args = parser.parse_args()

    scan_date = (datetime.strptime(args.date, "%Y-%m-%d").date()
                 if args.date else None)
    if args.symbol:
        describe_symbol(args.symbol.upper(), as_of=scan_date)
        return
    df = scan_market(scan_date=scan_date, top=args.top, min_history=args.min_history,
                     min_amount=args.min_amount, limit=args.limit, out_dir=args.out)
    if df.empty:
        sys.exit(1)


if __name__ == "__main__":
    main()
