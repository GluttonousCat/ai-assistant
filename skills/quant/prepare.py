"""Convert raw quotes to causal factor units; never fill absent bars or factors."""

from dataclasses import dataclass
import numpy as np
import pandas as pd
from skills.trend.strategy import TrendConfig, evaluated_bars


@dataclass
class MarketSeries:
    code: str
    name: str
    observed_at: pd.Timestamp
    times: pd.DatetimeIndex
    data: dict
    signals: list
    row_at: np.ndarray | None = None


def prepare_stock(raw, member, timeframe, cfg=TrendConfig()):
    if raw.empty:
        return None, "没有原生行情"
    f = raw.sort_values("bar_end").copy().reset_index(drop=True)
    if f.bar_end.duplicated().any():
        return None, "重复K线"
    numeric = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "adj_factor",
        "up_limit",
        "down_limit",
    ]
    f[numeric] = f[numeric].apply(pd.to_numeric, errors="coerce")
    if (
        not np.isfinite(f[numeric[:-2]]).all().all()
        or (f[["open", "high", "low", "close", "adj_factor"]] <= 0).any().any()
    ):
        return None, "价格/成交/复权数据不完整"
    if (
        (f.volume < 0).any()
        or (f.amount < 0).any()
        or (f.high < f[["open", "close", "low"]].max(axis=1)).any()
        or (f.low > f[["open", "close", "high"]].min(axis=1)).any()
    ):
        return None, "OHLC或成交数据无效"
    data = {k: f[k].to_numpy(dtype=float) for k in numeric if k != "adj_factor"}
    # Base on FIRST observed factor, avoiding a future normalization anchor.
    scale = f.adj_factor / f.adj_factor.iloc[0]
    for key in ["open", "high", "low", "close"]:
        f[key] *= scale
        data["adjusted_" + key] = f[key].to_numpy(dtype=float)
    data["scale"] = scale.to_numpy(dtype=float)
    evaluated = evaluated_bars(f, cfg, timeframe)
    signals = []
    for key, family in [("a", "trend_pullback"), ("b", "base_rebound")]:
        for i in np.flatnonzero(evaluated[f"{key}_eligible"].to_numpy()):
            row = evaluated.iloc[i]
            count = int(row[f"{key}_startup_count"])
            efficiency = float(row[f"{key}_startup_efficiency"])
            risk = float(row[f"{key}_risk"])
            signals.append(
                {
                    "i": int(i),
                    "family": family,
                    "preferred": row.pattern == family,
                    "leg": row[f"{key}_startup_start"].isoformat(),
                    "count": count,
                    "stop": float(row[f"{key}_stop"]),
                    "score": 40
                    + 30 * efficiency
                    + 20 * max(0, (5 - count) / 3)
                    + 10 * max(0, 1 - risk / cfg.max_risk_pct),
                }
            )
    data["trail_a"] = evaluated.ema_fast.to_numpy(dtype=float)
    data["trail_b"] = evaluated.ema_trigger.to_numpy(dtype=float)
    observed = pd.Timestamp(member["observed_at"])
    return MarketSeries(
        member["ts_code"],
        member["name"],
        observed,
        pd.DatetimeIndex(f.bar_end),
        data,
        signals,
    ), None
