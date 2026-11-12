"""Views schema definitions; SQL moved without changing table names."""
# Wide-table views (simplify Agent/LLM queries)
# ============================================================

DDL_VIEW_FINANCIAL_SUMMARY = """
CREATE OR REPLACE VIEW fin.v_financial_summary AS
SELECT
    i.ts_code,
    i.end_date,
    i.ann_date,
    i.total_revenue,
    i.revenue,
    i.operate_profit,
    i.total_profit,
    i.n_income,
    i.n_income_attr_p,
    i.basic_eps,
    i.rd_exp,
    b.total_assets,
    b.total_liab,
    b.total_hldr_eqy_exc_min_int,
    b.money_cap,
    b.accounts_receiv,
    b.inventories,
    b.goodwill,
    c.n_cashflow_act,
    c.n_cashflow_inv_act,
    c.n_cash_flows_fnc_act,
    f.roe,
    f.roe_waa,
    f.netprofit_yoy,
    f.or_yoy,
    f.grossprofit_margin,
    f.netprofit_margin,
    f.debt_to_assets,
    f.current_ratio,
    f.quick_ratio
FROM fin.income i
LEFT JOIN fin.balancesheet b
    ON i.ts_code = b.ts_code AND i.end_date = b.end_date AND i.report_type = b.report_type
LEFT JOIN fin.cashflow c
    ON i.ts_code = c.ts_code AND i.end_date = c.end_date AND i.report_type = c.report_type
LEFT JOIN fin.fina_indicator f
    ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
WHERE i.report_type = '1';
"""

DDL_VIEW_DAILY_VALUATION = """
CREATE OR REPLACE VIEW stock.v_daily_valuation AS
SELECT
    d.trade_date,
    d.ts_code,
    s.name,
    s.industry,
    d.open, d.high, d.low, d.close, d.vol, d.amount, d.pct_chg,
    db.turnover_rate,
    db.pe_ttm,
    db.pb,
    db.ps_ttm,
    db.total_mv,
    db.circ_mv
FROM stock.daily d
LEFT JOIN stock.daily_basic db
    ON d.trade_date = db.trade_date AND d.ts_code = db.ts_code
LEFT JOIN stock.stock_basic s
    ON d.ts_code = s.ts_code;
"""

VIEW_DDL = [DDL_VIEW_FINANCIAL_SUMMARY, DDL_VIEW_DAILY_VALUATION]


def init_views(pg_client) -> None:
    """Deploy the wide-table views (idempotent)"""
    for ddl in VIEW_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()


# ============================================================
