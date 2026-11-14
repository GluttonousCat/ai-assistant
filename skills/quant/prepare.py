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


def _normalized(raw):
    if raw.empty:
        return None, None, "没有原生行情"
    f = raw.sort_values("bar_end").copy().reset_index(drop=True)
    if f.bar_end.duplicated().any():
        return None, None, "重复K线"
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
        return None, None, "价格/成交/复权数据不完整"
    if (
        (f.volume < 0).any()
        or (f.amount < 0).any()
        or (f.high < f[["open", "close", "low"]].max(axis=1)).any()
        or (f.low > f[["open", "close", "high"]].min(axis=1)).any()
    ):
        return None, None, "OHLC或成交数据无效"
    # to_numpy may return a view: in-place factor adjustment below must NEVER
    # mutate raw execution prices. Keep raw and adjusted units disjoint.
    data = {
        k: f[k].to_numpy(dtype=float, copy=True) for k in numeric if k != "adj_factor"
    }
    # Base on FIRST observed factor, avoiding a future normalization anchor.
    scale = f.adj_factor / f.adj_factor.iloc[0]
    for key in ["open", "high", "low", "close"]:
        f[key] *= scale
        data["adjusted_" + key] = f[key].to_numpy(dtype=float)
    data["scale"] = scale.to_numpy(dtype=float)
    return f, data, None


def prepare_stock(raw, member, timeframe, cfg=TrendConfig()):
    f, data, issue = _normalized(raw)
    if issue:
        return None, issue
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
    data["atr"] = evaluated.atr.to_numpy(dtype=float)
    observed = pd.Timestamp(member["observed_at"])
    return MarketSeries(
        member["ts_code"],
        member["name"],
        observed,
        pd.DatetimeIndex(f.bar_end),
        data,
        signals,
    ), None


def prepare_mtf(minute, daily, member, cfg=None):
    from skills.trend.mtf import evaluate, MtfConfig

    m, _, issue = _normalized(minute)
    if issue:
        return None, "60F:" + issue
    d, data, issue = _normalized(daily)
    if issue:
        return None, "日K:" + issue
    cfg = cfg or MtfConfig()
    _, evaluated = evaluate(m, d, cfg)
    signals = []
    for i in np.flatnonzero(evaluated.eligible.to_numpy()):
        row = evaluated.iloc[i]
        signals.append(
            {
                "i": int(i),
                "family": row.pattern,
                "preferred": True,
                "leg": row.bar_end.isoformat(),
                "count": 1,
                "stop": float(row.stop_price),
                "score": float(row.score),
            }
        )
    data["trail_a"] = evaluated[f"ma{cfg.slow}"].to_numpy(dtype=float)
    data["trail_b"] = data["trail_a"].copy()
    data["atr"] = evaluated.atr.to_numpy(dtype=float)
    return MarketSeries(
        member["ts_code"],
        member["name"],
        pd.Timestamp(member["observed_at"]),
        pd.DatetimeIndex(d.bar_end),
        data,
        signals,
    ), None


def swing_frame(minute, frozen):
    """Validate native minutes and share the frozen daily factor base.

    Factors are keyed by date, never zipped. Minute/daily indicators share the
    frozen daily factor base, even if their first available dates differ.
    """
    m = minute.copy().sort_values("bar_end").reset_index(drop=True)
    m["bar_end"] = pd.to_datetime(m.bar_end)
    if m.empty or m.bar_end.duplicated().any():
        return None, "60F为空或重复"
    days = frozen.times.normalize()
    scale = pd.Series(frozen.data["scale"], index=days)
    m["scale"] = m.bar_end.dt.normalize().map(scale)
    fields = ["open", "high", "low", "close", "volume", "scale"]
    m[fields] = m[fields].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(m[fields]).all().all() or (m[fields] < 0).any().any():
        return None, "60F价格/成交/真实复权映射不完整"
    if (m[["open", "high", "low", "close", "scale"]] <= 0).any().any():
        return None, "60F非正价格/复权因子"
    if (
        (m.high < m[["open", "low", "close"]].max(axis=1))
        | (m.low > m[["open", "high", "close"]].min(axis=1))
    ).any():
        return None, "60F OHLC无效"
    for key in ["open", "high", "low", "close"]:
        m[key] *= m.scale
    return m, None


def prepare_swing(minute, frozen, cfg=None, evaluated=None):
    """60F-only decisions, using frozen daily execution inputs."""
    from skills.trend.swing import indicators, setups, execution_plans, SwingConfig

    cfg = cfg or SwingConfig()
    m, issue = swing_frame(minute, frozen)
    if issue:
        return None, issue
    f = evaluated if evaluated is not None else setups(indicators(m), cfg)
    daily = pd.DataFrame({"bar_end": frozen.times, "amount": frozen.data["amount"]})
    plans = execution_plans(f, daily, cfg)
    indexes = frozen.times.get_indexer(pd.DatetimeIndex(plans.bar_end))
    if (indexes < 0).any():
        return None, "60F信号缺少对应原生日K"
    signals = [
        dict(
            i=int(i),
            family=r.pattern,
            preferred=True,
            leg=r.signal_60f.isoformat(),
            count=1,
            stop=float(r.stop_price),
            score=float(r.score),
        )
        for i, r in zip(indexes, plans.itertuples())
    ]
    # join by time, not position: missing native slots never shift a MA.
    closing = f[f.bar_end.dt.hour.eq(15) & f.bar_end.dt.minute.eq(0)].set_index(
        "bar_end"
    )
    aligned = closing.reindex(frozen.times)
    data = dict(frozen.data)
    data["trail_a"] = aligned.ma21.to_numpy(dtype=float, copy=True)
    data["trail_b"] = data["trail_a"].copy()
    data["atr"] = aligned.atr.to_numpy(dtype=float, copy=True)
    return MarketSeries(
        frozen.code, frozen.name, frozen.observed_at, frozen.times.copy(), data, signals
    ), None
