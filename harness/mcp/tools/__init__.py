# -*- encoding: utf-8 -*-
"""
MCP tools package: 32 tools grouped into 5 domains (29 read-only + 3 write).

Importing this package completes all registration into REGISTRY:
    from harness.mcp.tools import REGISTRY
"""
from __future__ import annotations

from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolSpec

# Domain-grouped imports (import performs registration; same pattern as skills/registry)
from harness.mcp.tools import entity          # 8: resolve_stock / get_schema / trading_calendar / query_top_holders / query_industry / query_share_structure / get_session_history / make_table
from harness.mcp.tools import financial       # 8: query_financials / query_main_business / valuation_percentile / query_dividend / query_audit_opinion / disclosure_calendar / analyze_cashflow / dupont_analysis
from harness.mcp.tools import reports         # 7: search_reports / read_report / get_forecasts / summarize_sell_side / fetch_annual_report (write) / extract_document / extract_pdf_tables
from harness.mcp.tools import chain_alpha     # 5: list_chains / analyze_chain / forge_chain (write) / analyze_alpha / industry_peers
from harness.mcp.tools import risk            # 2: detect_financial_risk / verify_forecasts
from harness.mcp.tools import content         # 2: build_company_profile / write_article (write); registered under the report domain

__all__ = ["REGISTRY", "ToolSpec"]
