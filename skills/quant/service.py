"""Read-only quant application service; simulations stay in offline jobs."""

from skills.quant.registry import catalog
from tools.market.backtest_data import list_runs, load_run


def strategies():
    return catalog()


def results(limit=50):
    return list_runs(limit)


def result(run_id, offset=0, limit=100):
    return load_run(run_id, offset, limit)
