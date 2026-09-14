# -*- encoding: utf-8 -*-
"""
MCP 工具包: 22 个工具按 6 域分组 (19 只读 + 3 写类)

import 本包即完成全部注册 (REGISTRY):
    from core.mcp.tools import REGISTRY
"""
from __future__ import annotations

from core.mcp.registry import REGISTRY
from core.mcp.spec import ToolSpec

# 域分组 import (导入即注册, 与 skills/registry 同套路)
from core.mcp.tools import entity          # 3: resolve_stock / get_schema / trading_calendar
from core.mcp.tools import financial       # 3: query_financials / query_main_business / valuation_percentile
from core.mcp.tools import reports         # 6: search_reports / read_report / get_forecasts / fetch_annual_report(写) / extract_document / extract_pdf_tables
from core.mcp.tools import chain_alpha     # 4: list_chains / analyze_chain / forge_chain(写) / analyze_alpha
from core.mcp.tools import quant           # 2: compute_indicators / run_quant_scan
from core.mcp.tools import risk            # 2: detect_financial_risk / verify_forecasts
from core.mcp.tools import content         # 2: build_company_profile / write_article(写), 注册于 report 域

__all__ = ["REGISTRY", "ToolSpec"]
