"""Provider-free market boundary for strategy jobs and dashboard requests."""

from storage.repositories import trend as repository
from tools.market.queries import validate_intraday_request


def scan_inputs(codes, start, end):
    return repository.inputs(codes, start, end)


def scan_daily_inputs(codes, start, end):
    return repository.daily_inputs(codes, start, end)


def scan_universe(as_of, listed=False):
    return repository.universe(as_of, listed=listed) if listed else repository.universe(as_of)


def scan_cutoffs(now):
    return repository.cutoffs(now)


def load_dashboard(run_id=None, include_rows=True):
    return repository.latest_snapshot(run_id, include_rows)


def digest_stock_context(codes, trade_date):
    return repository.digest_stock_context(codes, trade_date)


def tracking_days(end):
    return repository.history_days(end)


def tracking_calendar(start, end):
    return repository.trading_days(start, end)


def tracking_history(end):
    return repository.tracking_history(end)


def intraday_report(run_id=None):
    from storage.repositories.intraday_watch import read_report
    return read_report(run_id)


def intraday_schedule():
    from storage.repositories.intraday_watch import scheduler_state
    return scheduler_state()


def refresh_freshness(snapshot=None):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from core.config import get_config
    from tools.market.refresh_clock import expected_date
    from tools.market.refresh_state import read_state

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    bounds = scan_cutoffs(now.replace(tzinfo=None))
    snapshot = snapshot if snapshot is not None else load_dashboard(include_rows=False)
    state = read_state()
    return {
        "expected_date": str(expected_date(now)),
        "warehouse": {key: str(value) if value is not None else None for key, value in bounds.items()},
        "snapshot_as_of": snapshot.get("as_of") if snapshot else None,
        "snapshot_run_id": snapshot.get("run_id") if snapshot else None,
        "schedule_enabled": bool(get_config().get("schedule.enabled", True)),
        "schedule_time": f'{int(get_config().get("schedule.sync_hour", 17)):02d}:{int(get_config().get("schedule.sync_minute", 0)):02d}',
        "time_zone": "Asia/Shanghai",
        "job": {key: state[key] for key in (
            "status", "stage", "target", "total", "completed", "failures", "updated_at",
            "coverage", "missing_60F", "reference_gaps", "last_success",
        ) if key in state},
    }


def adjusted_bars(code, start, end):
    # Reuse the canonical symbol/date validation, but bulk inputs keep factors.
    validate_intraday_request(code, start, end[:10], 60)
    return scan_inputs([code], start, end)


def adjusted_daily_bars(code, start, end):
    validate_intraday_request(code, start, end[:10], 60)
    return scan_daily_inputs([code], start, end)


def adjust_prices(frame):
    """Rebase to last observed factor; missing factors are never forward-filled.

    Using only factors attached to observed bar dates prevents ex-rights gaps
    from becoming false breakouts. Raw prices remain available for display.
    """
    import pandas as pd

    if frame.empty:
        return frame.copy(), "没有行情数据"
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
    if factors.isna().any():
        return f, "复权因子缺失"
    if not np.isfinite(factors).all() or (factors <= 0).any():
        return f, "复权因子无效"
    ratio = factors / factors.iloc[-1]
    prices = ["open", "high", "low", "close"]
    rebased = f[prices].mul(ratio, axis=0)
    if not np.isfinite(rebased).all().all() or (rebased <= 0).any().any():
        return f, "复权价格无效"
    f["raw_close"] = pd.to_numeric(f.close)
    f[prices] = rebased
    return f, None
