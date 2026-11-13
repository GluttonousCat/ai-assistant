"""Local-only quant data and results boundary."""

from storage.repositories.backtest import (
    quotes,
    benchmarks,
    list_runs,
    load_run,
    observed_dates,
    mark_suite_superseded,
)

__all__ = [
    "quotes",
    "benchmarks",
    "list_runs",
    "load_run",
    "observed_dates",
    "mark_suite_superseded",
]
