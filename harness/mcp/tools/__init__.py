# -*- encoding: utf-8 -*-
"""
MCP 工具包: 34 个工具按 6 域分组 (31 只读 + 3 写类)

import 本包即完成全部注册 (REGISTRY):
    from harness.mcp.tools import REGISTRY
"""
from __future__ import annotations

from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolSpec

# 域分组 import (导入即注册, 与 skills/registry 同套路)
from harness.mcp.tools import entity          # 8: resolve_stock / get_schema / trading_calendar / query_top_holders / query_industry / query_share_structure / get_session_history / make_table
from harness.mcp.tools import financial       # 8: query_financials / query_main_business / valuation_percentile / query_dividend / query_audit_opinion / disclosure_calendar / analyze_cashflow / dupont_analysis
from harness.mcp.tools import reports         # 7: search_reports / read_report / get_forecasts / summarize_sell_side / fetch_annual_report(写) / extract_document / extract_pdf_tables
from harness.mcp.tools import chain_alpha     # 5: list_chains / analyze_chain / forge_chain(写) / analyze_alpha / industry_peers
from harness.mcp.tools import quant           # 2: compute_indicators / run_quant_scan
from harness.mcp.tools import risk            # 2: detect_financial_risk / verify_forecasts
from harness.mcp.tools import content         # 2: build_company_profile / write_article(写), 注册于 report 域

__all__ = ["REGISTRY", "ToolSpec"]
