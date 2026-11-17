"""Compatibility exports; physical DDL is owned by storage.schema.industry."""

from tools.finance.chain_extract import DDL_CHAIN_EXTRACT, init_chain_extract_schema

__all__ = ["DDL_CHAIN_EXTRACT", "init_chain_extract_schema"]
