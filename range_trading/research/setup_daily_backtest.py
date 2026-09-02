# -*- encoding: utf-8 -*-
"""
蓄势识别器 逐日回测生成器 (2026-01-01 起)

【思路】(高效, 避免逐日重复全市场扫描):
  对全 universe 一次拉取 [start - 预热, 最后交易日 + 后续窗口] 的日K,
  每只股票只算一次 setup 特征序列 (滚动窗口可逐日递推),
  然后对 start 起的每个交易日 d:
    - 取该日 setup_state=DIGESTING 且 55<=base_score<80 的候选 (只用 d 及之前数据, 无前视);
    - 统计候选日之后 5/10/20 日的最大涨幅(MFE)、收盘涨幅、胜率、主升率;
  输出: 落盘 output/setup_scan/daily_backtest.csv (每候选日一行汇总 + 明细可选)。

用法:
    python -m range_trading.research.setup_daily_backtest
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

from core.logger import get_logger
from range_trading.data.loader import load_daily_bars, load_universe
from range_trading.features.base_setup import base_setup_score, is_candidate
from storage.pg import PgClient

logger = get_logger("range_trading.setup_backtest")
OUT_DIR = Path("output/setup_scan")

START_DATE = date(2026, 1, 1)     # 2026 起第一天
END_DATE = date(2026, 8, 24)
HORIZONS = (5, 10, 20)
MIN_SCORE, MAX_SCORE = 55.0, 80.0
WARMUP_DAYS = 300                  # 特征预热 (前复权 + 长窗口)

# 主升判定: horizon 内 MFE>=8% 且 MAE>=-5% (强势启动)
SURGE_MFE = 0.08
SURGE_MAE = -0.05


def _daily_candidates(df: pd.DataFrame, setup: pd.DataFrame) -> dict:
    """单股票: 返回 {trade_date: (setup_state, base_score)} 中 候选日的映射"""
    cand = is_candidate(setup, MIN_SCORE, MAX_SCORE).fillna(False)
    out = {}
    dates = pd.to_datetime(df["trade_date"].dt.strftime("%Y-%m-%d"))
    dstr = dates.dt.strftime("%Y-%m-%d")
    for i in range(len(df)):
        if bool(cand.iloc[i]):
            out[dstr.iloc[i]] = (setup["setup_state"].iloc[i], float(setup["base_score"].iloc[i]))
    return out


def run_daily_backtest(symbol_limit: Optional[int] = None) -> None:
    """全市场逐日回测, 落盘 daily_backtest.csv (按日汇总) + daily_backtest_detail.csv (候选明细)"""
    rows_detail: List[dict] = []

    with PgClient() as pg:
        universe = load_universe(pg, END_DATE, 120, 10000.0, 60)
        if symbol_limit:
            universe = universe[:symbol_limit]
        logger.info(f"universe {len(universe)} 只")

        start = START_DATE - timedelta(days=int(WARMUP_DAYS * 1.6))
        end = END_DATE + timedelta(days=60)   # 后续窗口
        logger.info(f"拉取窗口: {start} ~ {end}")

        # 分批拉取, 每批独立连接
        for i in range(0, len(universe), 200):
            batch = universe[i:i + 200]
            try:
                with PgClient() as pgt:
                    df_all = load_daily_bars(pgt, batch, start, end)
            except Exception as e:
                logger.warning(f"批次 {i // 200} 拉取失败跳过: {e}")
                continue
            if df_all.empty:
                continue
            for ts_code, g in df_all.groupby("ts_code", sort=False):
                g = g.sort_values("trade_date").reset_index(drop=True)
                if len(g) < 160:
                    continue
                try:
                    setup = base_setup_score(g["close"], g["high"], g["low"], g["vol"])
                except Exception:
                    continue
                cand_map = _daily_candidates(g, setup)
                if not cand_map:
                    continue
                # 逐日候选的前瞻表现
                dates_arr = pd.to_datetime(g["trade_date"].dt.strftime("%Y-%m-%d"))
                closes = g["close"].to_numpy(float)
                highs = g["high"].to_numpy(float)
                lows = g["low"].to_numpy(float)
                dlist = list(dates_arr)
                for dstr, (state, score) in cand_map.items():
                    try:
                        idx = dlist.index(pd.Timestamp(dstr))
                    except ValueError:
                        continue
                    for h in HORIZONS:
                        if idx + h >= len(g):
                            continue
                        entry = closes[idx]
                        if entry <= 0:
                            continue
                        fut_h = highs[idx + 1: idx + h + 1]
                        fut_l = lows[idx + 1: idx + h + 1]
                        mfe = float(fut_h.max() / entry - 1.0)
                        mae = float(fut_l.min() / entry - 1.0)
                        close_ret = float(closes[idx + h] / entry - 1.0)
                        surge = bool(mfe >= SURGE_MFE and mae >= SURGE_MAE)
                        rows_detail.append({
                            "date": dstr, "symbol": ts_code, "state": state,
                            "base_score": round(score, 1), "horizon": h,
                            "entry": round(float(entry), 3),
                            "mfe": round(mfe, 4), "mae": round(mae, 4),
                            "close_ret": round(close_ret, 4), "surge": surge,
                        })
            if i % 1000 == 0:
                logger.info(f"进度 {i + len(batch)}/{len(universe)}, 累计明细 {len(rows_detail)}")

    detail = pd.DataFrame(rows_detail)
    detail.to_csv(OUT_DIR / "daily_backtest_detail.csv", index=False, encoding="utf-8-sig")

    # ---- 按日汇总 (从 START_DATE 起, 每个候选日的整体表现) ----
    if detail.empty:
        logger.error("无候选产生")
        return
    detail = detail[detail["date"] >= START_DATE.strftime("%Y-%m-%d")]   # 7.21 起
    by_day = []
    for dstr, g in detail.groupby("date"):
        g5 = g[g["horizon"] == 5]
        g10 = g[g["horizon"] == 10]
        g20 = g[g["horizon"] == 20]

        def _agg(x):
            if x.empty:
                return None
            return {
                "n": len(x),
                "win_rate": round((x["close_ret"] > 0).mean(), 3),
                "surge_rate": round(x["surge"].mean(), 3),
                "avg_mfe": round(x["mfe"].mean(), 4),
                "avg_ret": round(x["close_ret"].mean(), 4),
            }

        row = {"date": dstr}
        for tag, gx in (("5d", g5), ("10d", g10), ("20d", g20)):
            a = _agg(gx)
            if a:
                row.update({f"{tag}_{k}": v for k, v in a.items()})
        by_day.append(row)

    summary = pd.DataFrame(by_day).sort_values("date").reset_index(drop=True)
    summary.to_csv(OUT_DIR / "daily_backtest.csv", index=False, encoding="utf-8-sig")
    logger.info(f"逐日回测完成: {len(summary)} 个候选日, 明细 {len(detail)} 行")
    print(summary.to_string(index=False))
    # 整体汇总
    print("\n===== 整体 (7.21~8.24 全部候选) =====")
    for h in HORIZONS:
        g = detail[detail["horizon"] == h]
        print(f"h={h}: n={len(g)} 胜率={ (g['close_ret']>0).mean():.1%}  主升率={g['surge'].mean():.1%}  "
              f"平均MFE={g['mfe'].mean():.1%}  平均收盘={g['close_ret'].mean():.1%}")


def main():
    parser = argparse.ArgumentParser(description="蓄势识别器 7.21 起逐日回测")
    parser.add_argument("--limit", type=int, default=None, help="限制 universe (调试)")
    args = parser.parse_args()
    run_daily_backtest(args.limit)


if __name__ == "__main__":
    main()