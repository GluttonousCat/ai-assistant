# -*- encoding: utf-8 -*-
"""
信号跟踪 (Signal Tracking): 每日扫描信号的事后表现回填与滚动统计

【定位】策略健康监控的实盘化 (指南第三十六节 Event Study 的落地):
- 每个信号日落盘一条信号快照 (state/score/区间/位置等);
- 随后每日回填该信号的事后区间维持度 (persist_ratio / drift / range_holding);
- 滚动胜率 = 最近 N 个信号日中, 高质量信号的 range_holding 比例。

数据表: stock.range_signal_tracking
  主键 (signal_date, symbol, horizon) — 同一信号按多个 horizon(5/10/20) 各存一行。

【边界约束】事后指标只在 horizon 完全成熟后才回填 (信号日 + horizon 个交易日之后),
每日调度时增量回填, 幂等 (ON CONFLICT 更新)。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from core.logger import get_logger

logger = get_logger("range_trading.tracking")

TRACK_TABLE = "range_signal_tracking"
HORIZONS = (5, 10, 20)

DDL = f"""
CREATE TABLE IF NOT EXISTS stock.{TRACK_TABLE} (
    signal_date   DATE NOT NULL,
    symbol        VARCHAR(16) NOT NULL,
    horizon       INT  NOT NULL,
    state         VARCHAR(32),
    range_score   DOUBLE PRECISION,
    range_pos     DOUBLE PRECISION,
    trend_risk    VARCHAR(8),
    upper         DOUBLE PRECISION,
    lower         DOUBLE PRECISION,
    width         DOUBLE PRECISION,
    close         DOUBLE PRECISION,
    -- 事后指标 (horizon 成熟后回填)
    persist_ratio   DOUBLE PRECISION,
    drift_natr      DOUBLE PRECISION,
    no_breakout     BOOLEAN,
    range_holding   BOOLEAN,
    fwd_ret         DOUBLE PRECISION,
    matured         BOOLEAN DEFAULT FALSE,
    updated_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (signal_date, symbol, horizon)
);
CREATE INDEX IF NOT EXISTS idx_rst_date ON stock.{TRACK_TABLE} (signal_date);
CREATE INDEX IF NOT EXISTS idx_rst_matured ON stock.{TRACK_TABLE} (matured);
"""


def ensure_table(pg) -> None:
    pg.execute(f"CREATE SCHEMA IF NOT EXISTS stock")
    for stmt in DDL.strip().split(";"):
        s = stmt.strip()
        if s:
            pg.execute(s)


# ---------------------------------------------------------------- 信号落盘

def record_signals(pg, scan_date: date, records: List[Dict[str, Any]]) -> int:
    """把某日扫描出的区间族信号落盘 (每个 horizon 一行, 事后指标留空待回填)"""
    from range_trading.regime.state_machine import RANGE_STATES
    ensure_table(pg)
    rows = []
    for r in records:
        if r.get("state") not in RANGE_STATES:
            continue
        for h in HORIZONS:
            rows.append({
                "signal_date": scan_date,
                "symbol": r["symbol"],
                "horizon": h,
                "state": r.get("state"),
                "range_score": _f(r.get("range_score")),
                "range_pos": _f(r.get("range_pos")),
                "trend_risk": r.get("trend_risk"),
                "upper": _f(r.get("upper")),
                "lower": _f(r.get("lower")),
                "width": _f(r.get("width")),
                "close": _f(r.get("close")),
                "matured": False,
            })
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    n = pg.upsert_df(df, "stock", TRACK_TABLE, conflict_keys=["signal_date", "symbol", "horizon"])
    logger.info(f"信号落盘 {scan_date}: {len(rows)} 行 ({len(df['symbol'].unique())} 只)")
    return n


# ---------------------------------------------------------------- 事后回填

def backfill_outcomes(pg, as_of: Optional[date] = None) -> int:
    """
    回填所有"已到成熟期但尚未回填"的信号事后指标。
    对每条未成熟信号: 若当前已有 signal_date + horizon 之后的数据, 则计算并更新。
    返回回填行数。
    """
    from range_trading.data.loader import load_daily_bars, get_latest_trade_date
    ensure_table(pg)
    if as_of is None:
        as_of = get_latest_trade_date(pg)

    pending = pg.fetch_all(
        f"SELECT DISTINCT signal_date, symbol FROM stock.{TRACK_TABLE} WHERE matured = FALSE"
    )
    if not pending:
        return 0

    # 按 symbol 分组, 一次拉全该股从最早信号日到 as_of 的行情
    sym_to_dates: Dict[str, List[date]] = {}
    for r in pending:
        sym_to_dates.setdefault(r["symbol"], []).append(r["signal_date"])

    updated = 0
    codes = sorted(sym_to_dates)
    batch = 200
    for i in range(0, len(codes), batch):
        sub = codes[i:i + batch]
        g_start = min(min(v) for v in (sym_to_dates[c] for c in sub)) - timedelta(days=5)
        df_all = load_daily_bars(pg, sub, g_start, as_of)
        if df_all.empty:
            continue
        for sym, g in df_all.groupby("ts_code", sort=False):
            g = g.sort_values("trade_date").reset_index(drop=True)
            dates = g["trade_date"].dt.date.values
            closes = g["close"].to_numpy(dtype=float)
            for sig_d in sym_to_dates.get(sym, []):
                idx = int(np.searchsorted(dates, sig_d))
                if idx >= len(g) or dates[idx] != sig_d:
                    continue
                # 取该信号在各 horizon 的行
                sig_rows = pg.fetch_all(
                    f"SELECT horizon, upper, lower, close FROM stock.{TRACK_TABLE} "
                    f"WHERE signal_date=%s AND symbol=%s AND matured=FALSE",
                    (sig_d, sym))
                for sr in sig_rows:
                    h = sr["horizon"]
                    if idx + h >= len(g):
                        continue  # 未来数据还不够, 留待下次
                    outcome = _compute_outcome(closes, idx, h, sr["upper"], sr["lower"])
                    if outcome is None:
                        continue
                    pg.execute(
                        f"UPDATE stock.{TRACK_TABLE} SET persist_ratio=%s, drift_natr=%s, "
                        f"no_breakout=%s, range_holding=%s, fwd_ret=%s, matured=TRUE, updated_at=now() "
                        f"WHERE signal_date=%s AND symbol=%s AND horizon=%s",
                        (outcome["persist_ratio"], outcome["drift_natr"], outcome["no_breakout"],
                         outcome["range_holding"], outcome["fwd_ret"], sig_d, sym, h))
                    updated += 1
    if updated:
        logger.info(f"事后回填完成: {updated} 行")
    return updated


def _compute_outcome(closes: np.ndarray, idx: int, horizon: int,
                     upper: float, lower: float) -> Optional[Dict[str, Any]]:
    """事后区间维持度 (与 event_study.forward_metrics 同口径, 基于收盘价)"""
    if upper is None or lower is None or not (upper > lower):
        return None
    close_t = closes[idx]
    fut = closes[idx + 1: idx + horizon + 1]
    if len(fut) < horizon:
        return None
    persist = float(np.mean((fut >= lower) & (fut <= upper)))
    width = upper - lower
    drift = abs(fut[-1] - close_t) / width if width > 0 else np.nan
    outside = (fut > upper) | (fut < lower)
    run = mx = 0
    for o in outside:
        run = run + 1 if o else 0
        mx = max(mx, run)
    no_breakout = mx < 2
    fwd_ret = float(fut[-1] / close_t - 1.0) if close_t > 0 else np.nan
    return {
        "persist_ratio": persist,
        "drift_natr": float(drift),
        "no_breakout": bool(no_breakout),
        "range_holding": bool(persist >= 0.55),
        "fwd_ret": fwd_ret,
    }


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if np.isnan(f) or np.isinf(f) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 滚动统计

def tracking_summary(pg, min_score: float = 70.0, horizon: int = 10,
                     lookback_days: int = 60) -> Dict[str, Any]:
    """滚动胜率统计: 按信号日聚合 + 分数档 + 状态 + 趋势风险分层"""
    ensure_table(pg)
    cutoff = as_of_str = None
    rows = pg.fetch_all(
        f"SELECT signal_date, symbol, state, range_score, range_pos, trend_risk, "
        f"persist_ratio, drift_natr, range_holding, fwd_ret "
        f"FROM stock.{TRACK_TABLE} WHERE horizon=%s AND matured=TRUE "
        f"ORDER BY signal_date",
        (horizon,))

    # 最近信号明细 (含未成熟, 便于看"进行中的信号") — 无论是否有成熟数据都返回
    recent_signals = pg.fetch_all(
        f"SELECT signal_date, symbol, state, range_score, range_pos, trend_risk, "
        f"upper, lower, close, persist_ratio, range_holding, fwd_ret, matured "
        f"FROM stock.{TRACK_TABLE} WHERE horizon=%s ORDER BY signal_date DESC, range_score DESC NULLS LAST LIMIT 100",
        (horizon,))
    for r in recent_signals:
        r["signal_date"] = r["signal_date"].strftime("%Y-%m-%d")

    if not rows:
        return {"horizon": horizon, "min_score": min_score, "by_date": [], "overall": {},
                "by_score_bin": [], "by_state": [], "by_risk": [],
                "recent_signals": recent_signals}
    df = pd.DataFrame(rows)
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df["hq"] = df["range_score"] >= min_score

    def agg(g):
        return {
            "n": int(len(g)),
            "holding_rate": round(float(g["range_holding"].mean()), 4),
            "avg_persist": round(float(g["persist_ratio"].mean()), 4),
            "avg_fwd_ret": round(float(g["fwd_ret"].mean()), 4),
        }

    # 按信号日 (全部 vs 高质量)
    by_date = []
    for d, g in df.groupby("signal_date"):
        hq = g[g["hq"]]
        by_date.append({
            "date": d.strftime("%Y-%m-%d"),
            "n": int(len(g)),
            "holding_rate": round(float(g["range_holding"].mean()), 4),
            "hq_n": int(len(hq)),
            "hq_holding_rate": round(float(hq["range_holding"].mean()), 4) if len(hq) else None,
        })

    # 分数档
    bins = [0, 55, 65, 75, 85, 200]
    labels = ["<55", "55-65", "65-75", "75-85", ">=85"]
    df["score_bin"] = pd.cut(df["range_score"], bins=bins, labels=labels, right=False)
    by_score_bin = [{"score_bin": str(k), **agg(g)} for k, g in df.groupby("score_bin", observed=True)]

    by_state = [{"state": k, **agg(g)} for k, g in df.groupby("state")]
    by_risk = [{"trend_risk": k, **agg(g)} for k, g in df.groupby("trend_risk")]

    # 滚动窗口 (最近 lookback_days 个信号日)
    recent = df[df["signal_date"] >= df["signal_date"].max() - pd.Timedelta(days=lookback_days)]
    overall = {
        "total_signals": int(len(df)),
        "matured": int(len(df)),
        "holding_rate_all": round(float(df["range_holding"].mean()), 4),
        "holding_rate_hq": round(float(df[df["hq"]]["range_holding"].mean()), 4) if df["hq"].any() else None,
        "recent_n": int(len(recent)),
        "recent_holding_rate": round(float(recent["range_holding"].mean()), 4) if len(recent) else None,
        "recent_hq_holding_rate": round(float(recent[recent["hq"]]["range_holding"].mean()), 4) if recent["hq"].any() else None,
        "first_date": df["signal_date"].min().strftime("%Y-%m-%d"),
        "last_date": df["signal_date"].max().strftime("%Y-%m-%d"),
    }

    return {
        "horizon": horizon, "min_score": min_score,
        "by_date": by_date, "overall": overall,
        "by_score_bin": by_score_bin, "by_state": by_state, "by_risk": by_risk,
        "recent_signals": recent_signals,
    }
