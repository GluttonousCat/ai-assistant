# -*- encoding: utf-8 -*-
"""
趋势信号跟踪 (Trend Signal Tracking)

对偶 research/signal_tracking.py (震荡版看 persist/holding),
趋势版看"方向是否兑现 + 途中回撤是否可控" (趋势指南第十三章、十六章):

  trend_mfe      入场后 N 日最大有利偏移 (相对入场价, %)
  trend_mae      入场后 N 日最大不利偏移 (%, 取负)
  profit_ratio   MFE / |MAE|  (趋势质量事后度量, 右尾收益的核心)
  trend_valid    Close_N/Close_0 > 1+2% 且 MAE 不超 1.5倍区间宽  (趋势持续标签)

数据表: stock.trend_signal_tracking  主键 (signal_date, symbol, horizon)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from core.logger import get_logger

logger = get_logger("range_trading.trend_tracking")

TRACK_TABLE = "trend_signal_tracking"
HORIZONS = (5, 10, 20)

DDL = f"""
CREATE TABLE IF NOT EXISTS stock.{TRACK_TABLE} (
    signal_date   DATE NOT NULL,
    symbol        VARCHAR(16) NOT NULL,
    horizon       INT  NOT NULL,
    state         VARCHAR(32),
    tqs           DOUBLE PRECISION,
    vpa           DOUBLE PRECISION,
    stage_label   VARCHAR(8),
    event         VARCHAR(16),
    pulse_share   DOUBLE PRECISION,
    entry_close   DOUBLE PRECISION,
    -- 事后指标 (趋势口径)
    trend_mfe     DOUBLE PRECISION,
    trend_mae     DOUBLE PRECISION,
    profit_ratio  DOUBLE PRECISION,
    trend_valid   BOOLEAN,
    fwd_ret       DOUBLE PRECISION,
    matured       BOOLEAN DEFAULT FALSE,
    updated_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (signal_date, symbol, horizon)
);
CREATE INDEX IF NOT EXISTS idx_tst_date ON stock.{TRACK_TABLE} (signal_date);
CREATE INDEX IF NOT EXISTS idx_tst_matured ON stock.{TRACK_TABLE} (matured);
"""


def ensure_table(pg) -> None:
    pg.execute("CREATE SCHEMA IF NOT EXISTS stock")
    for stmt in DDL.strip().split(";"):
        s = stmt.strip()
        if s:
            pg.execute(s)


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if np.isnan(f) or np.isinf(f) else f
    except (TypeError, ValueError):
        return None


def record_trend_signals(pg, scan_date: date, records: List[Dict[str, Any]]) -> int:
    """趋势信号落盘 (每个 horizon 一行)"""
    from range_trading.regime.state_machine import TREND_STATES
    ensure_table(pg)
    rows = []
    for r in records:
        if r.get("state") not in TREND_STATES:
            continue
        for h in HORIZONS:
            rows.append({
                "signal_date": scan_date,
                "symbol": r["symbol"],
                "horizon": h,
                "state": r.get("state"),
                "tqs": _f(r.get("tqs")),
                "vpa": _f(r.get("vpa")),
                "stage_label": r.get("stage_label"),
                "event": r.get("event"),
                "pulse_share": _f(r.get("pulse_share")),
                "entry_close": _f(r.get("close")),
                "matured": False,
            })
    if not rows:
        return 0
    n = pg.upsert_df(pd.DataFrame(rows), "stock", TRACK_TABLE,
                     conflict_keys=["signal_date", "symbol", "horizon"])
    logger.info(f"趋势信号落盘 {scan_date}: {len(rows)} 行")
    return n


def backfill_trend_outcomes(pg, as_of: Optional[date] = None) -> int:
    """回填已成熟趋势信号的事后指标 (MFE/MAE/profit_ratio/trend_valid)"""
    from range_trading.data.loader import load_daily_bars, get_latest_trade_date
    ensure_table(pg)
    if as_of is None:
        as_of = get_latest_trade_date(pg)

    pending = pg.fetch_all(
        f"SELECT DISTINCT signal_date, symbol FROM stock.{TRACK_TABLE} WHERE matured = FALSE")
    if not pending:
        return 0

    sym_to_dates: Dict[str, List[date]] = {}
    for r in pending:
        sym_to_dates.setdefault(r["symbol"], []).append(r["signal_date"])

    updated = 0
    codes = sorted(sym_to_dates)
    for i in range(0, len(codes), 200):
        sub = codes[i:i + 200]
        g_start = min(min(v) for v in (sym_to_dates[c] for c in sub)) - timedelta(days=5)
        df_all = load_daily_bars(pg, sub, g_start, as_of)
        if df_all.empty:
            continue
        for sym, g in df_all.groupby("ts_code", sort=False):
            g = g.sort_values("trade_date").reset_index(drop=True)
            dates = g["trade_date"].dt.date.values
            highs = g["high"].to_numpy(dtype=float)
            lows = g["low"].to_numpy(dtype=float)
            closes = g["close"].to_numpy(dtype=float)
            for sig_d in sym_to_dates.get(sym, []):
                idx = int(np.searchsorted(dates, sig_d))
                if idx >= len(g) or dates[idx] != sig_d:
                    continue
                sig_rows = pg.fetch_all(
                    f"SELECT horizon, entry_close FROM stock.{TRACK_TABLE} "
                    f"WHERE signal_date=%s AND symbol=%s AND matured=FALSE",
                    (sig_d, sym))
                for sr in sig_rows:
                    h = sr["horizon"]
                    if idx + h >= len(g):
                        continue
                    outcome = _compute_trend_outcome(highs, lows, closes, idx, h, sr["entry_close"])
                    if outcome is None:
                        continue
                    pg.execute(
                        f"UPDATE stock.{TRACK_TABLE} SET trend_mfe=%s, trend_mae=%s, "
                        f"profit_ratio=%s, trend_valid=%s, fwd_ret=%s, matured=TRUE, updated_at=now() "
                        f"WHERE signal_date=%s AND symbol=%s AND horizon=%s",
                        (outcome["trend_mfe"], outcome["trend_mae"], outcome["profit_ratio"],
                         outcome["trend_valid"], outcome["fwd_ret"], sig_d, sym, h))
                    updated += 1
    if updated:
        logger.info(f"趋势事后回填完成: {updated} 行")
    return updated


def _compute_trend_outcome(highs, lows, closes, idx, horizon, entry_close):
    """趋势事后指标 (以信号日收盘为基准, 次日开盘入场的近似用收盘口径)"""
    if entry_close is None or entry_close <= 0:
        return None
    fut_h = highs[idx + 1: idx + horizon + 1]
    fut_l = lows[idx + 1: idx + horizon + 1]
    if len(fut_h) < horizon:
        return None
    mfe = float(fut_h.max() / entry_close - 1.0)
    mae = float(fut_l.min() / entry_close - 1.0)
    fwd_ret = float(closes[idx + horizon] / entry_close - 1.0)
    profit_ratio = (mfe / abs(mae)) if mae < 0 else (mfe / 1e-6 if mfe > 0 else 0.0)
    # 趋势持续标签: 期末收益 > 2% 且 途中最大回撤 < MFE 的一半 (右尾兑现)
    trend_valid = bool(fwd_ret > 0.02 and abs(mae) < max(mfe, 1e-6))
    return {"trend_mfe": mfe, "trend_mae": mae, "profit_ratio": float(profit_ratio),
            "trend_valid": trend_valid, "fwd_ret": fwd_ret}


def trend_tracking_summary(pg, min_tqs: float = 55.0, horizon: int = 10) -> Dict[str, Any]:
    """趋势滚动统计: 按信号日 / TQS分档 / 状态 / 事件 分层, 看 MFE 与 profit_ratio"""
    ensure_table(pg)
    rows = pg.fetch_all(
        f"SELECT signal_date, symbol, state, tqs, vpa, stage_label, event, pulse_share, "
        f"trend_mfe, trend_mae, profit_ratio, trend_valid, fwd_ret "
        f"FROM stock.{TRACK_TABLE} WHERE horizon=%s AND matured=TRUE ORDER BY signal_date",
        (horizon,))

    recent_signals = pg.fetch_all(
        f"SELECT signal_date, symbol, state, tqs, vpa, stage_label, event, pulse_share, "
        f"entry_close, trend_mfe, trend_mae, profit_ratio, trend_valid, fwd_ret, matured "
        f"FROM stock.{TRACK_TABLE} WHERE horizon=%s "
        f"ORDER BY signal_date DESC, tqs DESC NULLS LAST LIMIT 100",
        (horizon,))
    for r in recent_signals:
        r["signal_date"] = r["signal_date"].strftime("%Y-%m-%d")

    base = {"horizon": horizon, "min_tqs": min_tqs, "by_date": [], "overall": {},
            "by_tqs_bin": [], "by_state": [], "by_event": [], "recent_signals": recent_signals}
    if not rows:
        return base

    df = pd.DataFrame(rows)
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df["hq"] = df["tqs"] >= min_tqs

    def agg(g):
        return {
            "n": int(len(g)),
            "valid_rate": round(float(g["trend_valid"].mean()), 4),
            "avg_mfe": round(float(g["trend_mfe"].mean()), 4),
            "avg_mae": round(float(g["trend_mae"].mean()), 4),
            "avg_profit_ratio": round(float(g["profit_ratio"].replace([np.inf, -np.inf], np.nan).mean()), 4),
            "avg_fwd_ret": round(float(g["fwd_ret"].mean()), 4),
        }

    by_date = []
    for d, g in df.groupby("signal_date"):
        hq = g[g["hq"]]
        by_date.append({
            "date": d.strftime("%Y-%m-%d"), "n": int(len(g)),
            "avg_mfe": round(float(g["trend_mfe"].mean()), 4),
            "valid_rate": round(float(g["trend_valid"].mean()), 4),
            "hq_n": int(len(hq)),
            "hq_avg_mfe": round(float(hq["trend_mfe"].mean()), 4) if len(hq) else None,
            "hq_valid_rate": round(float(hq["trend_valid"].mean()), 4) if len(hq) else None,
        })

    bins = [0, 40, 50, 60, 70, 80, 200]
    labels = ["<40", "40-50", "50-60", "60-70", "70-80", ">=80"]
    df["tqs_bin"] = pd.cut(df["tqs"], bins=bins, labels=labels, right=False)
    by_tqs_bin = [{"tqs_bin": str(k), **agg(g)} for k, g in df.groupby("tqs_bin", observed=True)]
    by_state = [{"state": k, **agg(g)} for k, g in df.groupby("state")]
    by_event = [{"event": k, **agg(g)} for k, g in df.groupby("event")]

    hq = df[df["hq"]]
    overall = {
        "total_signals": int(len(df)),
        "valid_rate_all": round(float(df["trend_valid"].mean()), 4),
        "avg_mfe_all": round(float(df["trend_mfe"].mean()), 4),
        "valid_rate_hq": round(float(hq["trend_valid"].mean()), 4) if len(hq) else None,
        "avg_mfe_hq": round(float(hq["trend_mfe"].mean()), 4) if len(hq) else None,
        "avg_profit_ratio_hq": round(float(hq["profit_ratio"].replace([np.inf, -np.inf], np.nan).mean()), 4) if len(hq) else None,
        "first_date": df["signal_date"].min().strftime("%Y-%m-%d"),
        "last_date": df["signal_date"].max().strftime("%Y-%m-%d"),
    }
    base.update({"by_date": by_date, "overall": overall,
                 "by_tqs_bin": by_tqs_bin, "by_state": by_state, "by_event": by_event})
    return base
