# -*- encoding: utf-8 -*-
"""
趋势行情全市场扫描 (趋势指南第十二章)

对偶 scanner/daily_scan.py。每天对全 universe 计算趋势 Regime,
输出 Ranking Table: 寻找"刚被量能确认、结构完整、处于早中期的趋势"。

用法:
    python -m range_trading.scanner.trend_scan --top 20
    python -m range_trading.scanner.trend_scan --symbol 000001.SZ
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
from range_trading.config import DEFAULT_TREND_CONFIG
from range_trading.data.loader import (
    estimate_start_date,
    get_latest_trade_date,
    iter_symbol_frames,
    load_daily_bars,
    load_universe,
)
from range_trading.regime.trend_regime import run_trend_regime
from range_trading.regime.state_machine import TREND_STATES
from storage.pg import PgClient

logger = get_logger("range_trading.trend_scan")


def scan_trend_market(scan_date: Optional[date] = None, top: int = 20,
                      min_amount: float = 10000.0,
                      limit: Optional[int] = None,
                      out_dir: str = "output/trend_scan") -> pd.DataFrame:
    """全市场趋势扫描, 返回 Ranking DataFrame 并落盘 CSV/JSON"""
    cfg = DEFAULT_TREND_CONFIG
    records: List[dict] = []
    with PgClient() as pg:
        if scan_date is None:
            scan_date = get_latest_trade_date(pg)
        if scan_date is None:
            logger.error("stock.daily 无数据")
            return pd.DataFrame()
        universe = load_universe(pg, scan_date, 200, min_amount, 60)
        if limit:
            universe = universe[:limit]
        logger.info(f"趋势扫描 {scan_date} | universe {len(universe)} 只")

        start = estimate_start_date(scan_date, lookback_days=300)
        n_done = n_skipped = 0
        for ts_code, df in iter_symbol_frames(pg, universe, start, scan_date, 300):
            n_done += 1
            if df["trade_date"].iloc[-1] != pd.Timestamp(scan_date):
                n_skipped += 1
                continue
            try:
                _, state = run_trend_regime(df, symbol=ts_code, config=cfg)
            except Exception as e:
                logger.warning(f"{ts_code} 趋势分析失败: {e}")
                continue
            if state is not None and state.state in TREND_STATES:
                records.append(state.to_dict())
        logger.info(f"完成: 分析 {n_done}, 跳过停牌 {n_skipped}, 趋势信号 {len(records)} 只")

    if not records:
        logger.warning("无趋势信号")
        return pd.DataFrame()

    result = pd.DataFrame(records).sort_values("tqs", ascending=False).reset_index(drop=True)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    tag = scan_date.strftime("%Y%m%d")
    result.to_csv(out_path / f"trend_scan_{tag}.csv", index=False, encoding="utf-8-sig")
    (out_path / f"trend_scan_{tag}.json").write_text(
        json.dumps(result.to_dict(orient="records"), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    print_ranking(result, top, scan_date)
    return result


def print_ranking(result: pd.DataFrame, top: int, scan_date) -> None:
    state_counts = result["state"].value_counts().to_dict()
    print("=" * 74)
    print(f"        TREND SCANNER   {scan_date}")
    print("=" * 74)
    print(f"状态分布: {dict(sorted(state_counts.items(), key=lambda x: -x[1]))}")
    print("-" * 74)
    for rank, rec in enumerate(result.head(top).to_dict(orient="records"), start=1):
        print(f"#{rank:<3d}{rec['symbol']:<12s}[{rec['state']}]")
        print(f"     TQS: {rec['tqs']:.0f}   VPA: {rec['vpa']:.2f}   Stage: {rec['stage_label']}"
              f"   Event: {rec['event']}")
        print(f"     ER20: {rec['er20']:.2f} (slope {rec['er_slope']:+.2f})"
              f"   UDVOL: {rec['udvol']:.2f}   CMF: {rec['cmf']:+.2f}"
              f"   VolRatio: {rec['vol_ratio']:.1f}")
        print(f"     PullbackDepth: {rec['pullback_depth']:.2f}   Integrity: {rec['integrity']:.2f}"
              f"   VWAPDev: {rec['vwap_dev_pct'] * 100:+.1f}%")
        print(f"     衰竭证据: {rec['exhaust_evidence']:.0f}"
              f"   背离: {'Y' if rec['bear_div'] else 'N'}   HL破坏: {'Y' if rec['hl_break'] else 'N'}"
              f"   Chandelier止损: {rec['chandelier_stop']:.2f}")
        print(f"     Close: {rec['close']:.2f}")
        print("-" * 74)


def main() -> None:
    parser = argparse.ArgumentParser(description="趋势行情 Scanner")
    parser.add_argument("--date", type=str, default=None, help="扫描日 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--limit", type=int, default=None, help="限制 universe 数量 (调试)")
    parser.add_argument("--min-amount", type=float, default=10000.0)
    parser.add_argument("--out", type=str, default="output/trend_scan")
    args = parser.parse_args()

    scan_date = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None)
    df = scan_trend_market(scan_date=scan_date, top=args.top,
                           min_amount=args.min_amount, limit=args.limit, out_dir=args.out)
    if df.empty:
        sys.exit(1)


if __name__ == "__main__":
    main()
