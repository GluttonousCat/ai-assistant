"""
Market data tools
- sync_tushare:   Tushare -> PostgreSQL full backfill
- sync_financial: financial statements incremental sync
- sync_mainbz:    main-business composition sync
"""
__all__ = ["TusharePgSyncer", "run_backfill"]


def __getattr__(name):
    """Keep historical exports without initializing unrelated sync modules."""
    if name in __all__:
        from tools.market import sync_tushare
        return getattr(sync_tushare, name)
    raise AttributeError(name)
