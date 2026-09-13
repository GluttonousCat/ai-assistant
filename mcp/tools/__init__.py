# -*- encoding: utf-8 -*-
"""
MCP 工具包: 19 个工具按 6 域分组

import 本包即完成全部注册 (REGISTRY):
    from mcp.tools import REGISTRY
"""
from __future__ import annotations

from mcp.registry import REGISTRY
from mcp.spec import ToolSpec

# 域分组 import (导入即注册, 与 skills/registry 同套路)
from mcp.tools import entity          # 3: resolve_stock / get_schema / trading_calendar
from mcp.tools import financial       # 3: query_financials / query_main_business / valuation_percentile
from mcp.tools import reports         # 5: search_reports / read_report / get_forecasts / extract_document / extract_pdf_tables
from mcp.tools import chain_alpha     # 4: list_chains / analyze_chain / forge_chain / analyze_alpha
from mcp.tools import quant           # 2: compute_indicators / run_quant_scan
from mcp.tools import risk            # 2: detect_financial_risk / verify_forecasts
from mcp.tools import content         # 2: build_company_profile / write_article (内容管线)

__all__ = ["REGISTRY", "ToolSpec"]
