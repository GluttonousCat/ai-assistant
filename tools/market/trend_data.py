"""Provider-free market boundary for strategy jobs and dashboard requests."""

from storage.repositories import trend as repository
from tools.market.queries import validate_intraday_request


def scan_inputs(codes, start, end):
    return repository.inputs(codes, start, end)


def scan_universe(as_of):
    return repository.universe(as_of)


def scan_cutoffs(now):
    return repository.cutoffs(now)


def load_dashboard(run_id=None, include_rows=True):
    return repository.latest_snapshot(run_id, include_rows)


def adjusted_bars(code, start, end):
    # Reuse the canonical symbol/date validation, but bulk inputs keep factors.
    validate_intraday_request(code, start, end[:10], 60)
    return scan_inputs([code], start, end)


def adjust_prices(frame):
    """Rebase to last observed factor; missing factors are never forward-filled.

    Using only factors attached to observed bar dates prevents ex-rights gaps
    from becoming false breakouts. Raw prices remain available for display.
    """
    import pandas as pd

    if frame.empty:
        return frame.copy(), "没有60F数据"
    f = frame.sort_values("bar_end").copy()
    for field in ("open", "high", "low", "close", "volume", "amount"):
        f[field] = pd.to_numeric(f[field], errors="coerce")
    import numpy as np

    if (
        not np.isfinite(f[["open", "high", "low", "close", "volume", "amount"]])
        .all()
        .all()
        or (f[["open", "high", "low", "close"]] <= 0).any().any()
    ):
        return f, "价格或成交数据无效"
    factors = pd.to_numeric(f.adj_factor, errors="coerce")
    if factors.isna().any() or (factors <= 0).any():
        return f, "复权因子缺失"
    ratio = factors / factors.iloc[-1]
    f["raw_close"] = pd.to_numeric(f.close)
    for field in ("open", "high", "low", "close"):
        f[field] = pd.to_numeric(f[field]) * ratio
    return f, None
