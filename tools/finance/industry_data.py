"""Industry analysis inputs; callers receive rows, never database handles."""

from storage.repositories.industry_data import (
    sw_members,
    stock_names,
    latest_main_business,
    recent_reports,
    chain_mentions,
    market_inputs,
)

__all__ = [
    "sw_members",
    "stock_names",
    "latest_main_business",
    "recent_reports",
    "chain_mentions",
    "market_inputs",
]
