"""Compatibility exports. Domain DDL is maintained in storage.schema.*.

Keep this module for existing CLI/tools imports; add new DDL to its domain.
"""

from storage.schema.connection import ddl_cursor

from storage.schema.stock import (
    DDL_STOCK_SCHEMA,
    DDL_STOCK_BASIC,
    DDL_TRADE_CALENDAR,
    DDL_DAILY,
    DDL_ADJ_FACTOR,
    DDL_DAILY_BASIC,
    DDL_SYNC_META,
    DDL_TOP10_HOLDERS,
    ALL_DDL,
    T_STOCK_BASIC,
    T_TRADE_CALENDAR,
    T_DAILY,
    T_ADJ_FACTOR,
    T_DAILY_BASIC,
    T_SYNC_META,
    T_TOP10_HOLDERS,
    init_schema,
)

from storage.schema.financial import (
    DDL_FIN_SCHEMA,
    DDL_INCOME,
    DDL_BALANCESHEET,
    DDL_CASHFLOW,
    DDL_FINA_INDICATOR,
    DDL_DIVIDEND,
    DDL_FINA_AUDIT,
    DDL_DISCLOSURE_DATE,
    DDL_FIN_SYNC_META,
    DDL_FINA_MAINBZ,
    DDL_VIEW_MAIN_BIZ,
    FIN_ALL_DDL,
    T_INCOME,
    T_BALANCESHEET,
    T_CASHFLOW,
    T_FINA_INDICATOR,
    T_DIVIDEND,
    T_FINA_AUDIT,
    T_DISCLOSURE_DATE,
    T_FINA_MAINBZ,
    T_FIN_SYNC_META,
    init_fin_schema,
)

from storage.schema.views import (
    DDL_VIEW_FINANCIAL_SUMMARY,
    DDL_VIEW_DAILY_VALUATION,
    VIEW_DDL,
    init_views,
)

from storage.schema.aliases import (
    DDL_STOCK_ALIAS,
    SEED_ALIASES,
)

from storage.schema.reports import (
    DDL_REPORT_META,
    DDL_REPORT_FORECAST,
    DDL_REPORT_SYNC_META,
    DDL_REPORT_MERGED,
    REPORT_DDL,
    T_REPORT_META,
    T_REPORT_FORECAST,
    T_REPORT_SYNC_META,
    init_report_schema,
    DDL_REPORT_ANALYSIS_STATEMENTS,
    ensure_report_analysis_columns,
)

from storage.schema.industry import (
    DDL_INDEX_CLASSIFY,
    DDL_STOCK_INDUSTRY,
)
