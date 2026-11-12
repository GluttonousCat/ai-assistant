"""Financial schema definitions; SQL moved without changing table names."""
# Financial data warehouse (schema: fin)
# ============================================================
# Three financial statements + financial indicators + dividends/splits + audit
# opinions + disclosure dates + main-business composition, 1:1 with all fields of
# the official docs
# (income 94 / balancesheet 158 / cashflow 97 / fina_indicator 167 + local report_type /
#  dividend 16 / fina_audit 7 / disclosure_date 6 / fina_mainbz 9, 2026-10 official
#  output parameter tables;
#  the legacy DB once had a subset with misnamed columns — migrated to full set and dead columns dropped)

DDL_FIN_SCHEMA = """CREATE SCHEMA IF NOT EXISTS fin"""

# ---------- Income statement ----------
DDL_INCOME = """
CREATE TABLE IF NOT EXISTS fin.income (
    ts_code          VARCHAR(16) NOT NULL,   -- TS code
    ann_date         DATE,                   -- announcement date
    f_ann_date       DATE,                   -- actual announcement date
    end_date         DATE NOT NULL,          -- report period
    report_type      VARCHAR(2) NOT NULL DEFAULT '1',  -- report type 1 consolidated / 6 parent / 4 adjusted-consolidated
    comp_type        VARCHAR(2),             -- company type 1 general / 2 bank / 3 insurance / 4 securities
    end_type         VARCHAR(2),             -- report period type
    basic_eps        NUMERIC(20,6),          -- basic EPS
    diluted_eps      NUMERIC(20,6),          -- diluted EPS
    total_revenue    NUMERIC(24,4),          -- total operating revenue
    revenue          NUMERIC(24,4),          -- operating revenue
    int_income       NUMERIC(24,4),          -- interest income
    prem_earned      NUMERIC(24,4),          -- earned premiums
    comm_income      NUMERIC(24,4),          -- fees & commission income
    n_commis_income  NUMERIC(24,4),          -- net fees & commission income
    n_oth_income     NUMERIC(24,4),          -- net other operating income
    n_oth_b_income   NUMERIC(24,4),          -- add: net income from other operations
    prem_income      NUMERIC(24,4),          -- insurance business income
    out_prem         NUMERIC(24,4),          -- less: premiums ceded
    une_prem_reser   NUMERIC(24,4),          -- unexpired risk reserves withdrawn
    reins_income     NUMERIC(24,4),          -- of which: reinsurance premium income
    n_sec_tb_income  NUMERIC(24,4),          -- net income from securities brokerage (trading)
    n_sec_uw_income  NUMERIC(24,4),          -- net income from securities underwriting
    n_asset_mg_income NUMERIC(24,4),         -- net income from client asset management
    oth_b_income     NUMERIC(24,4),          -- other business income
    fv_value_chg_gain NUMERIC(24,4),         -- add: net gains from changes in fair value
    invest_income    NUMERIC(24,4),          -- add: net investment income
    ass_invest_income NUMERIC(24,4),         -- of which: investment income from associates & JVs
    forex_gain       NUMERIC(24,4),          -- add: net foreign-exchange gains
    total_cogs       NUMERIC(24,4),          -- total operating cost
    oper_cost        NUMERIC(24,4),          -- less: operating cost
    int_exp          NUMERIC(24,4),          -- less: interest expense
    comm_exp         NUMERIC(24,4),          -- less: fees & commission expense
    biz_tax_surchg   NUMERIC(24,4),          -- less: business taxes & surcharges
    sell_exp         NUMERIC(24,4),          -- less: selling expense
    admin_exp        NUMERIC(24,4),          -- less: administrative expense
    fin_exp          NUMERIC(24,4),          -- less: financial expense
    assets_impair_loss NUMERIC(24,4),        -- less: asset impairment loss
    prem_refund      NUMERIC(24,4),          -- insurance refunds
    compens_payout   NUMERIC(24,4),          -- total claims paid
    reser_insur_liab NUMERIC(24,4),          -- insurance reserves withdrawn
    div_payt         NUMERIC(24,4),          -- policyholder dividend expense
    reins_exp        NUMERIC(24,4),          -- reinsurance expense
    oper_exp         NUMERIC(24,4),          -- operating expense
    compens_payout_refu NUMERIC(24,4),       -- less: claims recovered
    insur_reser_refu NUMERIC(24,4),          -- less: insurance reserves recovered
    reins_cost_refund NUMERIC(24,4),         -- less: reinsurance cost recovered
    other_bus_cost   NUMERIC(24,4),          -- other business cost
    operate_profit   NUMERIC(24,4),          -- operating profit
    non_oper_income  NUMERIC(24,4),          -- add: non-operating income
    non_oper_exp     NUMERIC(24,4),          -- less: non-operating expense
    nca_disploss     NUMERIC(24,4),          -- of which: less: net loss on disposal of non-current assets
    total_profit     NUMERIC(24,4),          -- total profit
    income_tax       NUMERIC(24,4),          -- income tax expense
    n_income         NUMERIC(24,4),          -- net profit (incl. minority interest)
    n_income_attr_p  NUMERIC(24,4),          -- net profit (excl. minority interest)
    minority_gain    NUMERIC(24,4),          -- minority interest income
    oth_compr_income NUMERIC(24,4),          -- other comprehensive income
    t_compr_income   NUMERIC(24,4),          -- total comprehensive income
    compr_inc_attr_p NUMERIC(24,4),          -- total comprehensive income attributable to parent
    compr_inc_attr_m_s NUMERIC(24,4),        -- total comprehensive income attributable to minority
    ebit             NUMERIC(24,4),          -- EBIT
    ebitda           NUMERIC(24,4),          -- EBITDA
    insurance_exp    NUMERIC(24,4),          -- insurance business expense
    undist_profit    NUMERIC(24,4),          -- undistributed profit at year start
    distable_profit  NUMERIC(24,4),          -- distributable profit
    rd_exp           NUMERIC(24,4),          -- R&D expense
    fin_exp_int_exp  NUMERIC(24,4),          -- financial expense: interest expense
    fin_exp_int_inc  NUMERIC(24,4),          -- financial expense: interest income
    transfer_surplus_rese NUMERIC(24,4),     -- transferred from surplus reserve
    transfer_housing_imprest NUMERIC(24,4),  -- transferred from housing revolving fund
    transfer_oth     NUMERIC(24,4),          -- other transfers-in
    adj_lossgain     NUMERIC(24,4),          -- prior-year profit/loss adjustments
    withdra_legal_surplus NUMERIC(24,4),     -- statutory surplus reserve withdrawn
    withdra_legal_pubfund NUMERIC(24,4),     -- statutory public welfare fund withdrawn
    withdra_biz_devfund NUMERIC(24,4),       -- business development fund withdrawn
    withdra_rese_fund NUMERIC(24,4),         -- reserve fund withdrawn
    withdra_oth_ersu NUMERIC(24,4),          -- discretionary surplus reserve withdrawn
    workers_welfare  NUMERIC(24,4),          -- staff bonus & welfare
    distr_profit_shrhder NUMERIC(24,4),      -- profit distributable to shareholders
    prfshare_payable_dvd NUMERIC(24,4),      -- preferred share dividends payable
    comshare_payable_dvd NUMERIC(24,4),      -- common share dividends payable
    capit_comstock_div NUMERIC(24,4),        -- common share dividends converted to share capital
    net_after_nr_lp_correct NUMERIC(24,4),   -- net profit after non-recurring items (before correction)
    credit_impa_loss NUMERIC(24,4),          -- credit impairment loss
    net_expo_hedging_benefits NUMERIC(24,4), -- net open-position hedging gains
    oth_impair_loss_assets NUMERIC(24,4),    -- other asset impairment loss
    total_opcost     NUMERIC(24,4),          -- total operating cost (II)
    amodcost_fin_assets NUMERIC(24,4),       -- derecognition gains on financial assets at amortized cost
    oth_income       NUMERIC(24,4),          -- other income
    asset_disp_income NUMERIC(24,4),         -- asset disposal income
    continued_net_profit NUMERIC(24,4),      -- net profit from continuing operations
    end_net_profit   NUMERIC(24,4),          -- net profit from discontinued operations
    update_flag      VARCHAR(4),             -- update flag
    created_at       TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.income IS '利润表(全字段)';
"""


# ---------- Balance sheet (1:1 with Tushare's official 158 fields, 2026-10 official output parameter table) ----------
DDL_BALANCESHEET = """
CREATE TABLE IF NOT EXISTS fin.balancesheet (
    ts_code                     VARCHAR(16) NOT NULL,      -- TS code
    ann_date                    DATE,                      -- announcement date
    f_ann_date                  DATE,                      -- actual announcement date
    end_date                    DATE NOT NULL,             -- report period
    report_type                 VARCHAR(2) NOT NULL DEFAULT '1', -- report type
    comp_type                   VARCHAR(2),                -- company type (1 general, 2 bank, 3 insurance, 4 securities, 7 diversified financials)
    end_type                    VARCHAR(2),                -- report period type
    total_share                 NUMERIC(24,4),             -- total shares at period end (shares)
    cap_rese                    NUMERIC(24,4),             -- capital reserve
    undistr_porfit              NUMERIC(24,4),             -- undistributed profit
    surplus_rese                NUMERIC(24,4),             -- surplus reserve
    special_rese                NUMERIC(24,4),             -- special reserve
    money_cap                   NUMERIC(24,4),             -- cash
    trad_asset                  NUMERIC(24,4),             -- trading financial assets
    notes_receiv                NUMERIC(24,4),             -- notes receivable
    accounts_receiv             NUMERIC(24,4),             -- accounts receivable
    oth_receiv                  NUMERIC(24,4),             -- other receivables
    prepayment                  NUMERIC(24,4),             -- prepayments
    div_receiv                  NUMERIC(24,4),             -- dividends receivable
    int_receiv                  NUMERIC(24,4),             -- interest receivable
    inventories                 NUMERIC(24,4),             -- inventories
    amor_exp                    NUMERIC(24,4),             -- deferred expenses
    nca_within_1y               NUMERIC(24,4),             -- non-current assets due within 1 year
    sett_rsrv                   NUMERIC(24,4),             -- settlement reserves
    loanto_oth_bank_fi          NUMERIC(24,4),             -- placements with other banks/FIs
    premium_receiv              NUMERIC(24,4),             -- premiums receivable
    reinsur_receiv              NUMERIC(24,4),             -- reinsurance receivables
    reinsur_res_receiv          NUMERIC(24,4),             -- reinsurance contract reserves receivable
    pur_resale_fa               NUMERIC(24,4),             -- financial assets bought under resale agreements
    oth_cur_assets              NUMERIC(24,4),             -- other current assets
    total_cur_assets            NUMERIC(24,4),             -- total current assets
    fa_avail_for_sale           NUMERIC(24,4),             -- financial assets available for sale
    htm_invest                  NUMERIC(24,4),             -- held-to-maturity investments
    lt_eqt_invest               NUMERIC(24,4),             -- long-term equity investments
    invest_real_estate          NUMERIC(24,4),             -- investment properties
    time_deposits               NUMERIC(24,4),             -- time deposits
    oth_assets                  NUMERIC(24,4),             -- other assets
    lt_rec                      NUMERIC(24,4),             -- long-term receivables
    fix_assets                  NUMERIC(24,4),             -- fixed assets
    cip                         NUMERIC(24,4),             -- construction in progress
    const_materials             NUMERIC(24,4),             -- construction materials
    fixed_assets_disp           NUMERIC(24,4),             -- fixed assets held for disposal
    produc_bio_assets           NUMERIC(24,4),             -- productive biological assets
    oil_and_gas_assets          NUMERIC(24,4),             -- oil and gas assets
    intan_assets                NUMERIC(24,4),             -- intangible assets
    r_and_d                     NUMERIC(24,4),             -- R&D expenditure
    goodwill                    NUMERIC(24,4),             -- goodwill
    lt_amor_exp                 NUMERIC(24,4),             -- long-term deferred expenses
    defer_tax_assets            NUMERIC(24,4),             -- deferred tax assets
    decr_in_disbur              NUMERIC(24,4),             -- loans and advances issued
    oth_nca                     NUMERIC(24,4),             -- other non-current assets
    total_nca                   NUMERIC(24,4),             -- total non-current assets
    cash_reser_cb               NUMERIC(24,4),             -- cash and balances with central bank
    depos_in_oth_bfi            NUMERIC(24,4),             -- placements with banks and other FIs
    prec_metals                 NUMERIC(24,4),             -- precious metals
    deriv_assets                NUMERIC(24,4),             -- derivative financial assets
    rr_reins_une_prem           NUMERIC(24,4),             -- reinsurance unearned premium reserves receivable
    rr_reins_outstd_cla         NUMERIC(24,4),             -- reinsurance outstanding claims reserves receivable
    rr_reins_lins_liab          NUMERIC(24,4),             -- reinsurance life insurance reserves receivable
    rr_reins_lthins_liab        NUMERIC(24,4),             -- reinsurance long-term health insurance reserves receivable
    refund_depos                NUMERIC(24,4),             -- deposits paid out
    ph_pledge_loans             NUMERIC(24,4),             -- policyholder pledge loans
    refund_cap_depos            NUMERIC(24,4),             -- capital deposits paid out
    indep_acct_assets           NUMERIC(24,4),             -- separate account assets
    client_depos                NUMERIC(24,4),             -- of which: client deposits
    client_prov                 NUMERIC(24,4),             -- of which: client provision
    transac_seat_fee            NUMERIC(24,4),             -- of which: trading seat fees
    invest_as_receiv            NUMERIC(24,4),             -- investments classified as receivables
    total_assets                NUMERIC(24,4),             -- total assets
    lt_borr                     NUMERIC(24,4),             -- long-term borrowings
    st_borr                     NUMERIC(24,4),             -- short-term borrowings
    cb_borr                     NUMERIC(24,4),             -- borrowings from central bank
    depos_ib_deposits           NUMERIC(24,4),             -- deposits and interbank placements taken
    loan_oth_bank               NUMERIC(24,4),             -- placements taken
    trading_fl                  NUMERIC(24,4),             -- trading financial liabilities
    notes_payable               NUMERIC(24,4),             -- notes payable
    acct_payable                NUMERIC(24,4),             -- accounts payable
    adv_receipts                NUMERIC(24,4),             -- advances from customers
    sold_for_repur_fa           NUMERIC(24,4),             -- financial assets sold under repurchase agreements
    comm_payable                NUMERIC(24,4),             -- fees & commissions payable
    payroll_payable             NUMERIC(24,4),             -- payroll payable
    taxes_payable               NUMERIC(24,4),             -- taxes payable
    int_payable                 NUMERIC(24,4),             -- interest payable
    div_payable                 NUMERIC(24,4),             -- dividends payable
    oth_payable                 NUMERIC(24,4),             -- other payables
    acc_exp                     NUMERIC(24,4),             -- accrued expenses
    deferred_inc                NUMERIC(24,4),             -- deferred income
    st_bonds_payable            NUMERIC(24,4),             -- short-term bonds payable
    payable_to_reinsurer        NUMERIC(24,4),             -- reinsurance payables
    rsrv_insur_cont             NUMERIC(24,4),             -- insurance contract reserves
    acting_trading_sec          NUMERIC(24,4),             -- securities brokerage (trading) payables
    acting_uw_sec               NUMERIC(24,4),             -- securities underwriting payables
    non_cur_liab_due_1y         NUMERIC(24,4),             -- non-current liabilities due within 1 year
    oth_cur_liab                NUMERIC(24,4),             -- other current liabilities
    total_cur_liab              NUMERIC(24,4),             -- total current liabilities
    bond_payable                NUMERIC(24,4),             -- bonds payable
    lt_payable                  NUMERIC(24,4),             -- long-term payables
    specific_payables           NUMERIC(24,4),             -- specific payables
    estimated_liab              NUMERIC(24,4),             -- estimated liabilities
    defer_tax_liab              NUMERIC(24,4),             -- deferred tax liabilities
    defer_inc_non_cur_liab      NUMERIC(24,4),             -- deferred income - non-current liabilities
    oth_ncl                     NUMERIC(24,4),             -- other non-current liabilities
    total_ncl                   NUMERIC(24,4),             -- total non-current liabilities
    depos_oth_bfi               NUMERIC(24,4),             -- deposits of banks and other FIs
    deriv_liab                  NUMERIC(24,4),             -- derivative financial liabilities
    depos                       NUMERIC(24,4),             -- deposits taken
    agency_bus_liab             NUMERIC(24,4),             -- agency business liabilities
    oth_liab                    NUMERIC(24,4),             -- other liabilities
    prem_receiv_adva            NUMERIC(24,4),             -- advances on premiums
    depos_received              NUMERIC(24,4),             -- deposits received
    ph_invest                   NUMERIC(24,4),             -- policyholder deposits and investments
    reser_une_prem              NUMERIC(24,4),             -- unearned premium reserves
    reser_outstd_claims         NUMERIC(24,4),             -- outstanding claims reserves
    reser_lins_liab             NUMERIC(24,4),             -- life insurance reserves
    reser_lthins_liab           NUMERIC(24,4),             -- long-term health insurance reserves
    indept_acc_liab             NUMERIC(24,4),             -- separate account liabilities
    pledge_borr                 NUMERIC(24,4),             -- of which: pledged borrowings
    indem_payable               NUMERIC(24,4),             -- indemnity payables
    policy_div_payable          NUMERIC(24,4),             -- policyholder dividends payable
    total_liab                  NUMERIC(24,4),             -- total liabilities
    treasury_share              NUMERIC(24,4),             -- less: treasury shares
    ordin_risk_reser            NUMERIC(24,4),             -- general risk reserve
    forex_differ                NUMERIC(24,4),             -- foreign-currency translation difference
    invest_loss_unconf          NUMERIC(24,4),             -- unrecognized investment losses
    minority_int                NUMERIC(24,4),             -- minority interest
    total_hldr_eqy_exc_min_int  NUMERIC(24,4),             -- total shareholders' equity (excl. minority interest)
    total_hldr_eqy_inc_min_int  NUMERIC(24,4),             -- total shareholders' equity (incl. minority interest)
    total_liab_hldr_eqy         NUMERIC(24,4),             -- total liabilities and shareholders' equity
    lt_payroll_payable          NUMERIC(24,4),             -- long-term payroll payable
    oth_comp_income             NUMERIC(24,4),             -- other comprehensive income
    oth_eqt_tools               NUMERIC(24,4),             -- other equity instruments
    oth_eqt_tools_p_shr         NUMERIC(24,4),             -- other equity instruments (preferred shares)
    lending_funds               NUMERIC(24,4),             -- funds lent
    acc_receivable              NUMERIC(24,4),             -- receivables
    st_fin_payable              NUMERIC(24,4),             -- short-term financing payables
    payables                    NUMERIC(24,4),             -- payables
    hfs_assets                  NUMERIC(24,4),             -- assets held for sale
    hfs_sales                   NUMERIC(24,4),             -- liabilities held for sale
    cost_fin_assets             NUMERIC(24,4),             -- financial assets at amortized cost
    fair_value_fin_assets       NUMERIC(24,4),             -- financial assets at fair value with changes in other comprehensive income
    cip_total                   NUMERIC(24,4),             -- construction in progress (total) (CNY)
    oth_pay_total               NUMERIC(24,4),             -- other payables (total) (CNY)
    long_pay_total              NUMERIC(24,4),             -- long-term payables (total) (CNY)
    debt_invest                 NUMERIC(24,4),             -- debt investments (CNY)
    oth_debt_invest             NUMERIC(24,4),             -- other debt investments (CNY)
    oth_eq_invest               NUMERIC(24,4),             -- other equity instrument investments (CNY)
    oth_illiq_fin_assets        NUMERIC(24,4),             -- other non-current financial assets (CNY)
    oth_eq_ppbond               NUMERIC(24,4),             -- other equity instruments: perpetual bonds (CNY)
    receiv_financing            NUMERIC(24,4),             -- receivables financing
    use_right_assets            NUMERIC(24,4),             -- right-of-use assets
    lease_liab                  NUMERIC(24,4),             -- lease liabilities
    contract_assets             NUMERIC(24,4),             -- contract assets
    contract_liab               NUMERIC(24,4),             -- contract liabilities
    accounts_receiv_bill        NUMERIC(24,4),             -- notes and accounts receivable
    accounts_pay                NUMERIC(24,4),             -- notes and accounts payable
    oth_rcv_total               NUMERIC(24,4),             -- other receivables (total) (CNY)
    fix_assets_total            NUMERIC(24,4),             -- fixed assets (total) (CNY)
    update_flag                 VARCHAR(4),
    created_at                  TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.balancesheet IS '资产负债表(Tushare官方158字段1:1)';
"""

# ---------- Cash flow statement (1:1 with Tushare's official 97 fields, 2026-10 official output parameter table) ----------
DDL_CASHFLOW = """
CREATE TABLE IF NOT EXISTS fin.cashflow (
    ts_code          VARCHAR(16) NOT NULL,     -- TS code
    ann_date         DATE,                     -- announcement date
    f_ann_date       DATE,                     -- actual announcement date
    end_date         DATE NOT NULL,            -- report period
    report_type      VARCHAR(2) NOT NULL DEFAULT '1', -- report type
    comp_type        VARCHAR(2),               -- company type (1 general, 2 bank, 3 insurance, 4 securities, 7 diversified financials)
    end_type         VARCHAR(2),               -- report period type
    net_profit       NUMERIC(24,4),            -- net profit
    finan_exp        NUMERIC(24,4),            -- financial expense
    c_fr_sale_sg     NUMERIC(24,4),            -- cash received from sales of goods and services
    recp_tax_rends   NUMERIC(24,4),            -- tax refunds received
    n_depos_incr_fi  NUMERIC(24,4),            -- net increase in customer deposits and interbank placements
    n_incr_loans_cb  NUMERIC(24,4),            -- net increase in borrowings from central bank
    n_inc_borr_oth_fi NUMERIC(24,4),           -- net increase in funds borrowed from other FIs
    prem_fr_orig_contr NUMERIC(24,4),          -- cash received from original insurance contract premiums
    n_incr_insured_dep NUMERIC(24,4),          -- net increase in policyholder deposits
    n_reinsur_prem   NUMERIC(24,4),            -- net cash received from reinsurance business
    n_incr_disp_tfa  NUMERIC(24,4),            -- net increase in disposal of trading financial assets
    ifc_cash_incr    NUMERIC(24,4),            -- net increase in interest and fees collected
    n_incr_disp_faas NUMERIC(24,4),            -- net increase in disposal of available-for-sale financial assets
    n_incr_loans_oth_bank NUMERIC(24,4),       -- net increase in placements taken from other banks
    n_cap_incr_repur NUMERIC(24,4),            -- net increase in repurchase business funds
    c_fr_oth_operate_a NUMERIC(24,4),          -- other cash received relating to operating activities
    c_inf_fr_operate_a NUMERIC(24,4),          -- subtotal of operating cash inflows
    c_paid_goods_s   NUMERIC(24,4),            -- cash paid for goods and services
    c_paid_to_for_empl NUMERIC(24,4),          -- cash paid to and on behalf of employees
    c_paid_for_taxes NUMERIC(24,4),            -- taxes paid
    n_incr_clt_loan_adv NUMERIC(24,4),         -- net increase in client loans and advances
    n_incr_dep_cbob  NUMERIC(24,4),            -- net increase in deposits with central bank and other banks
    c_pay_claims_orig_inco NUMERIC(24,4),      -- cash paid for original insurance contract claims
    pay_handling_chrg NUMERIC(24,4),           -- cash paid for handling fees
    pay_comm_insur_plcy NUMERIC(24,4),         -- cash paid for policyholder dividends
    oth_cash_pay_oper_act NUMERIC(24,4),       -- other cash paid relating to operating activities
    st_cash_out_act  NUMERIC(24,4),            -- subtotal of operating cash outflows
    n_cashflow_act   NUMERIC(24,4),            -- net cash flow from operating activities
    oth_recp_ral_inv_act NUMERIC(24,4),        -- other cash received relating to investing activities
    c_disp_withdrwl_invest NUMERIC(24,4),      -- cash received from investment recoveries
    c_recp_return_invest NUMERIC(24,4),        -- cash received from investment income
    n_recp_disp_fiolta NUMERIC(24,4),          -- net cash from disposal of fixed assets, intangibles and other long-term assets
    n_recp_disp_sobu NUMERIC(24,4),            -- net cash received from disposal of subsidiaries and other business units
    stot_inflows_inv_act NUMERIC(24,4),        -- subtotal of investing cash inflows
    c_pay_acq_const_fiolta NUMERIC(24,4),      -- cash paid for fixed assets, intangibles and other long-term assets
    c_paid_invest    NUMERIC(24,4),            -- cash paid for investments
    n_disp_subs_oth_biz NUMERIC(24,4),         -- net cash paid for acquisitions of subsidiaries and other business units
    oth_pay_ral_inv_act NUMERIC(24,4),         -- other cash paid relating to investing activities
    n_incr_pledge_loan NUMERIC(24,4),          -- net increase in pledged loans
    stot_out_inv_act NUMERIC(24,4),            -- subtotal of investing cash outflows
    n_cashflow_inv_act NUMERIC(24,4),          -- net cash flow from investing activities
    c_recp_borrow    NUMERIC(24,4),            -- cash received from borrowings
    proc_issue_bonds NUMERIC(24,4),            -- cash received from bond issuance
    oth_cash_recp_ral_fnc_act NUMERIC(24,4),   -- other cash received relating to financing activities
    stot_cash_in_fnc_act NUMERIC(24,4),        -- subtotal of financing cash inflows
    free_cashflow    NUMERIC(24,4),            -- free cash flow to firm
    c_prepay_amt_borr NUMERIC(24,4),           -- cash paid for debt repayment
    c_pay_dist_dpcp_int_exp NUMERIC(24,4),     -- cash paid for dividends, profit distributions and interest
    incl_dvd_profit_paid_sc_ms NUMERIC(24,4),  -- of which: dividends/profit paid by subsidiaries to minority shareholders
    oth_cashpay_ral_fnc_act NUMERIC(24,4),     -- other cash paid relating to financing activities
    stot_cashout_fnc_act NUMERIC(24,4),        -- subtotal of financing cash outflows
    n_cash_flows_fnc_act NUMERIC(24,4),        -- net cash flow from financing activities
    eff_fx_flu_cash  NUMERIC(24,4),            -- effect of exchange-rate changes on cash
    n_incr_cash_cash_equ NUMERIC(24,4),        -- net increase in cash and cash equivalents
    c_cash_equ_beg_period NUMERIC(24,4),       -- cash and cash equivalents at period beginning
    c_cash_equ_end_period NUMERIC(24,4),       -- cash and cash equivalents at period end
    c_recp_cap_contrib NUMERIC(24,4),          -- cash received from capital contributions
    incl_cash_rec_saims NUMERIC(24,4),         -- of which: cash received by subsidiaries from minority shareholders
    uncon_invest_loss NUMERIC(24,4),           -- unrecognized investment loss
    prov_depr_assets NUMERIC(24,4),            -- add: provision for asset impairment
    depr_fa_coga_dpba NUMERIC(24,4),           -- depreciation of fixed assets, depletion of oil & gas assets, depreciation of productive biological assets
    amort_intang_assets NUMERIC(24,4),         -- amortization of intangible assets
    lt_amort_deferred_exp NUMERIC(24,4),       -- amortization of long-term deferred expenses
    decr_deferred_exp NUMERIC(24,4),           -- decrease in deferred expenses
    incr_acc_exp     NUMERIC(24,4),            -- increase in accrued expenses
    loss_disp_fiolta NUMERIC(24,4),            -- loss on disposal of fixed, intangible and other long-term assets
    loss_scr_fa      NUMERIC(24,4),            -- loss on scrapping of fixed assets
    loss_fv_chg      NUMERIC(24,4),            -- loss from changes in fair value
    invest_loss      NUMERIC(24,4),            -- investment loss
    decr_def_inc_tax_assets NUMERIC(24,4),     -- decrease in deferred tax assets
    incr_def_inc_tax_liab NUMERIC(24,4),       -- increase in deferred tax liabilities
    decr_inventories NUMERIC(24,4),            -- decrease in inventories
    decr_oper_payable NUMERIC(24,4),           -- decrease in operating receivables
    incr_oper_payable NUMERIC(24,4),           -- increase in operating payables
    others           NUMERIC(24,4),            -- others
    im_net_cashflow_oper_act NUMERIC(24,4),    -- net cash flow from operating activities (indirect method)
    conv_debt_into_cap NUMERIC(24,4),          -- debt converted into capital
    conv_copbonds_due_within_1y NUMERIC(24,4), -- convertible corporate bonds due within 1 year
    fa_fnc_leases    NUMERIC(24,4),            -- fixed assets acquired under finance leases
    im_n_incr_cash_equ NUMERIC(24,4),          -- net increase in cash and cash equivalents (indirect method)
    net_dism_capital_add NUMERIC(24,4),        -- net increase in funds lent out
    net_cash_rece_sec NUMERIC(24,4),           -- net cash received from securities brokerage (CNY)
    credit_impa_loss NUMERIC(24,4),            -- credit impairment loss
    use_right_asset_dep NUMERIC(24,4),         -- depreciation of right-of-use assets
    oth_loss_asset   NUMERIC(24,4),            -- other asset impairment loss
    end_bal_cash     NUMERIC(24,4),            -- cash at period end
    beg_bal_cash     NUMERIC(24,4),            -- less: cash at period beginning
    end_bal_cash_equ NUMERIC(24,4),            -- add: cash equivalents at period end
    beg_bal_cash_equ NUMERIC(24,4),            -- less: cash equivalents at period beginning
    update_flag      VARCHAR(4),               -- update flag (1 = latest)
    created_at       TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.cashflow IS '现金流量表(Tushare官方97字段1:1)';
"""

# ---------- Financial indicators (1:1 with Tushare's official 167 fields + local report_type PK column, 2026-10 official site) ----------
DDL_FINA_INDICATOR = """
CREATE TABLE IF NOT EXISTS fin.fina_indicator (
    ts_code                       VARCHAR(16) NOT NULL,       -- TS code
    ann_date                      DATE,                       -- announcement date
    end_date                      DATE NOT NULL,              -- report period
    report_type                   VARCHAR(2) NOT NULL DEFAULT '1', -- local PK column (not an official output; do not drop)
    eps                           NUMERIC(20,6),              -- basic EPS
    dt_eps                        NUMERIC(20,6),              -- diluted EPS
    total_revenue_ps              NUMERIC(20,6),              -- total operating revenue per share
    revenue_ps                    NUMERIC(20,6),              -- operating revenue per share
    capital_rese_ps               NUMERIC(20,6),              -- capital reserve per share
    surplus_rese_ps               NUMERIC(20,6),              -- surplus reserve per share
    undist_profit_ps              NUMERIC(20,6),              -- undistributed profit per share
    extra_item                    NUMERIC(20,6),              -- non-recurring profit/loss
    profit_dedt                   NUMERIC(20,6),              -- net profit after deducting non-recurring items
    gross_margin                  NUMERIC(20,6),              -- gross profit
    current_ratio                 NUMERIC(20,6),              -- current ratio
    quick_ratio                   NUMERIC(20,6),              -- quick ratio
    cash_ratio                    NUMERIC(20,6),              -- conservative quick ratio
    invturn_days                  NUMERIC(20,6),              -- inventory turnover days
    arturn_days                   NUMERIC(20,6),              -- accounts receivable turnover days
    inv_turn                      NUMERIC(20,6),              -- inventory turnover
    ar_turn                       NUMERIC(20,6),              -- accounts receivable turnover
    ca_turn                       NUMERIC(20,6),              -- current assets turnover
    fa_turn                       NUMERIC(20,6),              -- fixed assets turnover
    assets_turn                   NUMERIC(20,6),              -- total assets turnover
    op_income                     NUMERIC(20,6),              -- net income from operating activities
    valuechange_income            NUMERIC(20,6),              -- net income from value changes
    interst_income                NUMERIC(20,6),              -- interest expense
    daa                           NUMERIC(20,6),              -- depreciation and amortization
    ebit                          NUMERIC(20,6),              -- EBIT
    ebitda                        NUMERIC(20,6),              -- EBITDA
    fcff                          NUMERIC(20,6),              -- free cash flow to firm
    fcfe                          NUMERIC(20,6),              -- free cash flow to equity
    current_exint                 NUMERIC(20,6),              -- non-interest-bearing current liabilities
    noncurrent_exint              NUMERIC(20,6),              -- non-interest-bearing non-current liabilities
    interestdebt                  NUMERIC(20,6),              -- interest-bearing debt
    netdebt                       NUMERIC(20,6),              -- net debt
    tangible_asset                NUMERIC(20,6),              -- tangible assets
    working_capital               NUMERIC(20,6),              -- working capital
    networking_capital            NUMERIC(20,6),              -- net working capital
    invest_capital                NUMERIC(20,6),              -- total invested capital
    retained_earnings             NUMERIC(20,6),              -- retained earnings
    diluted2_eps                  NUMERIC(20,6),              -- period-end diluted EPS
    bps                           NUMERIC(20,6),              -- book value per share
    ocfps                         NUMERIC(20,6),              -- net operating cash flow per share
    retainedps                    NUMERIC(20,6),              -- retained earnings per share
    cfps                          NUMERIC(20,6),              -- net cash flow per share
    ebit_ps                       NUMERIC(20,6),              -- EBIT per share
    fcff_ps                       NUMERIC(20,6),              -- FCFF per share
    fcfe_ps                       NUMERIC(20,6),              -- FCFE per share
    netprofit_margin              NUMERIC(20,6),              -- net profit margin
    grossprofit_margin            NUMERIC(20,6),              -- gross profit margin
    cogs_of_sales                 NUMERIC(20,6),              -- cost of sales ratio
    expense_of_sales              NUMERIC(20,6),              -- operating expense ratio
    profit_to_gr                  NUMERIC(20,6),              -- net profit / total operating revenue
    saleexp_to_gr                 NUMERIC(20,6),              -- selling expense / total operating revenue
    adminexp_of_gr                NUMERIC(20,6),              -- administrative expense / total operating revenue
    finaexp_of_gr                 NUMERIC(20,6),              -- financial expense / total operating revenue
    impai_ttm                     NUMERIC(20,6),              -- asset impairment loss / total operating revenue
    gc_of_gr                      NUMERIC(20,6),              -- total operating cost / total operating revenue
    op_of_gr                      NUMERIC(20,6),              -- operating profit / total operating revenue
    ebit_of_gr                    NUMERIC(20,6),              -- EBIT / total operating revenue
    roe                           NUMERIC(20,6),              -- return on equity
    roe_waa                       NUMERIC(20,6),              -- weighted average ROE
    roe_dt                        NUMERIC(20,6),              -- ROE after deducting non-recurring items
    roa                           NUMERIC(20,6),              -- return on total assets
    npta                          NUMERIC(20,6),              -- net profit on total assets
    roic                          NUMERIC(20,6),              -- return on invested capital
    roe_yearly                    NUMERIC(20,6),              -- annualized ROE
    roa2_yearly                   NUMERIC(20,6),              -- annualized return on total assets
    roe_avg                       NUMERIC(20,6),              -- average ROE (rights-issue condition)
    opincome_of_ebt               NUMERIC(20,6),              -- net operating income / total profit
    investincome_of_ebt           NUMERIC(20,6),              -- net value-change income / total profit
    n_op_profit_of_ebt            NUMERIC(20,6),              -- net non-operating income / total profit
    tax_to_ebt                    NUMERIC(20,6),              -- income tax / total profit
    dtprofit_to_profit            NUMERIC(20,6),              -- net profit after non-recurring items / net profit
    salescash_to_or               NUMERIC(20,6),              -- cash from sales of goods and services / operating revenue
    ocf_to_or                     NUMERIC(20,6),              -- net operating cash flow / operating revenue
    ocf_to_opincome               NUMERIC(20,6),              -- net operating cash flow / net operating income
    capitalized_to_da             NUMERIC(20,6),              -- capital expenditure / depreciation and amortization
    debt_to_assets                NUMERIC(20,6),              -- debt-to-asset ratio
    assets_to_eqt                 NUMERIC(20,6),              -- equity multiplier
    dp_assets_to_eqt              NUMERIC(20,6),              -- equity multiplier (DuPont analysis)
    ca_to_assets                  NUMERIC(20,6),              -- current assets / total assets
    nca_to_assets                 NUMERIC(20,6),              -- non-current assets / total assets
    tbassets_to_totalassets       NUMERIC(20,6),              -- tangible assets / total assets
    int_to_talcap                 NUMERIC(20,6),              -- interest-bearing debt / total invested capital
    eqt_to_talcapital             NUMERIC(20,6),              -- equity attributable to parent / total invested capital
    currentdebt_to_debt           NUMERIC(20,6),              -- current liabilities / total liabilities
    longdeb_to_debt               NUMERIC(20,6),              -- non-current liabilities / total liabilities
    ocf_to_shortdebt              NUMERIC(20,6),              -- net operating cash flow / current liabilities
    debt_to_eqt                   NUMERIC(20,6),              -- debt-to-equity ratio
    eqt_to_debt                   NUMERIC(20,6),              -- equity attributable to parent / total liabilities
    eqt_to_interestdebt           NUMERIC(20,6),              -- equity attributable to parent / interest-bearing debt
    tangibleasset_to_debt         NUMERIC(20,6),              -- tangible assets / total liabilities
    tangasset_to_intdebt          NUMERIC(20,6),              -- tangible assets / interest-bearing debt
    tangibleasset_to_netdebt      NUMERIC(20,6),              -- tangible assets / net debt
    ocf_to_debt                   NUMERIC(20,6),              -- net operating cash flow / total liabilities
    ocf_to_interestdebt           NUMERIC(20,6),              -- net operating cash flow / interest-bearing debt
    ocf_to_netdebt                NUMERIC(20,6),              -- net operating cash flow / net debt
    ebit_to_interest              NUMERIC(20,6),              -- interest coverage (EBIT / interest expense)
    longdebt_to_workingcapital    NUMERIC(20,6),              -- long-term debt to working capital ratio
    ebitda_to_debt                NUMERIC(20,6),              -- EBITDA / total liabilities
    turn_days                     NUMERIC(20,6),              -- operating cycle
    roa_yearly                    NUMERIC(20,6),              -- annualized return on total assets
    roa_dp                        NUMERIC(20,6),              -- return on total assets (DuPont analysis)
    fixed_assets                  NUMERIC(20,6),              -- total fixed assets
    profit_prefin_exp             NUMERIC(20,6),              -- operating profit before financial expense
    non_op_profit                 NUMERIC(20,6),              -- non-operating profit
    op_to_ebt                     NUMERIC(20,6),              -- operating profit / total profit
    nop_to_ebt                    NUMERIC(20,6),              -- non-operating profit / total profit
    ocf_to_profit                 NUMERIC(20,6),              -- net operating cash flow / operating profit
    cash_to_liqdebt               NUMERIC(20,6),              -- cash / current liabilities
    cash_to_liqdebt_withinterest  NUMERIC(20,6),              -- cash / interest-bearing current liabilities
    op_to_liqdebt                 NUMERIC(20,6),              -- operating profit / current liabilities
    op_to_debt                    NUMERIC(20,6),              -- operating profit / total liabilities
    roic_yearly                   NUMERIC(20,6),              -- annualized ROIC
    total_fa_trun                 NUMERIC(20,6),              -- total fixed assets turnover
    profit_to_op                  NUMERIC(20,6),              -- total profit / operating revenue
    q_opincome                    NUMERIC(20,6),              -- net operating income (single quarter)
    q_investincome                NUMERIC(20,6),              -- net value-change income (single quarter)
    q_dtprofit                    NUMERIC(20,6),              -- net profit after non-recurring items (single quarter)
    q_eps                         NUMERIC(20,6),              -- EPS (single quarter)
    q_netprofit_margin            NUMERIC(20,6),              -- net profit margin (single quarter)
    q_gsprofit_margin             NUMERIC(20,6),              -- gross profit margin (single quarter)
    q_exp_to_sales                NUMERIC(20,6),              -- operating expense ratio (single quarter)
    q_profit_to_gr                NUMERIC(20,6),              -- net profit / total operating revenue (single quarter)
    q_saleexp_to_gr               NUMERIC(20,6),              -- selling expense / total operating revenue (single quarter)
    q_adminexp_to_gr              NUMERIC(20,6),              -- administrative expense / total operating revenue (single quarter)
    q_finaexp_to_gr               NUMERIC(20,6),              -- financial expense / total operating revenue (single quarter)
    q_impair_to_gr_ttm            NUMERIC(20,6),              -- asset impairment loss / total operating revenue (single quarter)
    q_gc_to_gr                    NUMERIC(20,6),              -- total operating cost / total operating revenue (single quarter)
    q_op_to_gr                    NUMERIC(20,6),              -- operating profit / total operating revenue (single quarter)
    q_roe                         NUMERIC(20,6),              -- ROE (single quarter)
    q_dt_roe                      NUMERIC(20,6),              -- single-quarter ROE after non-recurring items
    q_npta                        NUMERIC(20,6),              -- net profit on total assets (single quarter)
    q_opincome_to_ebt             NUMERIC(20,6),              -- net operating income / total profit (single quarter)
    q_investincome_to_ebt         NUMERIC(20,6),              -- net value-change income / total profit (single quarter)
    q_dtprofit_to_profit          NUMERIC(20,6),              -- net profit after non-recurring items / net profit (single quarter)
    q_salescash_to_or             NUMERIC(20,6),              -- cash from sales of goods and services / operating revenue (single quarter)
    q_ocf_to_sales                NUMERIC(20,6),              -- net operating cash flow / operating revenue (single quarter)
    q_ocf_to_or                   NUMERIC(20,6),              -- net operating cash flow / net operating income (single quarter)
    basic_eps_yoy                 NUMERIC(20,6),              -- basic EPS YoY growth (%)
    dt_eps_yoy                    NUMERIC(20,6),              -- diluted EPS YoY growth (%)
    cfps_yoy                      NUMERIC(20,6),              -- net operating cash flow per share YoY growth (%)
    op_yoy                        NUMERIC(20,6),              -- operating profit YoY growth (%)
    ebt_yoy                       NUMERIC(20,6),              -- total profit YoY growth (%)
    netprofit_yoy                 NUMERIC(20,6),              -- net profit attributable to parent YoY growth (%)
    dt_netprofit_yoy              NUMERIC(20,6),              -- net profit attributable to parent after non-recurring items YoY growth (%)
    ocf_yoy                       NUMERIC(20,6),              -- net operating cash flow YoY growth (%)
    roe_yoy                       NUMERIC(20,6),              -- ROE (diluted) YoY growth (%)
    bps_yoy                       NUMERIC(20,6),              -- BPS growth vs year start (%)
    assets_yoy                    NUMERIC(20,6),              -- total assets growth vs year start (%)
    eqt_yoy                       NUMERIC(20,6),              -- equity attributable to parent growth vs year start (%)
    tr_yoy                        NUMERIC(20,6),              -- total operating revenue YoY growth (%)
    or_yoy                        NUMERIC(20,6),              -- operating revenue YoY growth (%)
    q_gr_yoy                      NUMERIC(20,6),              -- total operating revenue YoY growth (%) (single quarter)
    q_gr_qoq                      NUMERIC(20,6),              -- total operating revenue QoQ growth (%) (single quarter)
    q_sales_yoy                   NUMERIC(20,6),              -- operating revenue YoY growth (%) (single quarter)
    q_sales_qoq                   NUMERIC(20,6),              -- operating revenue QoQ growth (%) (single quarter)
    q_op_yoy                      NUMERIC(20,6),              -- operating profit YoY growth (%) (single quarter)
    q_op_qoq                      NUMERIC(20,6),              -- operating profit QoQ growth (%) (single quarter)
    q_profit_yoy                  NUMERIC(20,6),              -- net profit YoY growth (%) (single quarter)
    q_profit_qoq                  NUMERIC(20,6),              -- net profit QoQ growth (%) (single quarter)
    q_netprofit_yoy               NUMERIC(20,6),              -- net profit attributable to parent YoY growth (%) (single quarter)
    q_netprofit_qoq               NUMERIC(20,6),              -- net profit attributable to parent QoQ growth (%) (single quarter)
    equity_yoy                    NUMERIC(20,6),              -- net assets YoY growth
    rd_exp                        NUMERIC(20,6),              -- total R&D investment
    update_flag                   VARCHAR(4),                 -- update flag
    created_at                    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.fina_indicator IS '财务指标(Tushare官方167字段1:1, 另含本地report_type主键列)';
"""

# ---------- Dividends/splits (1:1 with Tushare's official 16 fields, 2026-10 official output parameter table) ----------
# No official unique key: PK is (ts_code, end_date, ann_date, div_proc) — all four are
# officially "shown as Y" by default;
# multi-stage rows (proposal/approval/implementation) are told apart by div_proc,
# multiple dividends in one year by ann_date;
# implementation date columns (ex_date etc.) are NULL at the proposal stage and must
# not enter the PK.
# Field note: a few early-stage rows (e.g. "shareholder proposal") actually have NULL
# ann_date; the sync layer skips such rows (PK columns must be non-null)
DDL_DIVIDEND = """
CREATE TABLE IF NOT EXISTS fin.dividend (
    ts_code       VARCHAR(16) NOT NULL,   -- TS code
    end_date      DATE NOT NULL,          -- dividend year
    ann_date      DATE NOT NULL,          -- announcement date (proposal, resolution)
    div_proc      VARCHAR(16) NOT NULL,   -- implementation stage
    stk_div       NUMERIC(12,6),          -- shares transferred per share
    stk_bo_rate   NUMERIC(12,6),          -- bonus share ratio per share
    stk_co_rate   NUMERIC(12,6),          -- capitalization-of-reserves ratio per share
    cash_div      NUMERIC(12,6),          -- cash dividend per share (after tax)
    cash_div_tax  NUMERIC(12,6),          -- cash dividend per share (before tax)
    record_date   DATE,                   -- record date
    ex_date       DATE,                   -- ex-dividend/ex-rights date
    pay_date      DATE,                   -- payment date
    div_listdate  DATE,                   -- bonus share listing date
    imp_ann_date  DATE,                   -- implementation announcement date
    base_date     DATE,                   -- base date
    base_share    NUMERIC(20,4),          -- base share count (10k)
    created_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, ann_date, div_proc)
);
COMMENT ON TABLE fin.dividend IS '分红送股(Tushare官方16字段1:1)';
"""

# ---------- Financial audit opinions (1:1 with Tushare's official 7 fields, 2026-10 official output parameter table) ----------
# No official unique key: PK is (ts_code, end_date, ann_date) — all three always have values;
# annual report corrections/restatements announce audit opinions again for the same
# end_date; ann_date keeps the history of opinion changes;
# to get the latest opinion, take max(ann_date) within (ts_code, end_date)
DDL_FINA_AUDIT = """
CREATE TABLE IF NOT EXISTS fin.fina_audit (
    ts_code       VARCHAR(16) NOT NULL,   -- TS code
    ann_date      DATE NOT NULL,          -- announcement date
    end_date      DATE NOT NULL,          -- report period
    audit_result  VARCHAR(64),            -- audit result
    audit_fees    NUMERIC(20,4),          -- total audit fees (CNY)
    audit_agency  VARCHAR(128),           -- accounting firm
    audit_sign    VARCHAR(128),           -- signing CPAs
    created_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, ann_date)
);
COMMENT ON TABLE fin.fina_audit IS '财务审计意见(Tushare官方7字段1:1)';
"""

# ---------- Financial report disclosure dates (1:1 with Tushare's official 6 fields, 2026-10 official output parameter table) ----------
# No official unique key: PK is (ts_code, end_date) — one disclosure plan per stock per
# report period (latest status row);
# ann_date "latest disclosure announcement date" / modify_date "disclosure date correction
# record" both have same-row overwrite semantics (a correction adds no new row);
# ann_date is not guaranteed non-null (a scheduled plan can exist before any
# announcement), actual_date is NULL before disclosure — neither may enter the PK
DDL_DISCLOSURE_DATE = """
CREATE TABLE IF NOT EXISTS fin.disclosure_date (
    ts_code       VARCHAR(16) NOT NULL,   -- TS code
    ann_date      DATE,                   -- latest disclosure announcement date
    end_date      DATE NOT NULL,          -- report period
    pre_date      DATE,                   -- scheduled disclosure date
    actual_date   DATE,                   -- actual disclosure date
    modify_date   DATE,                   -- disclosure date correction record
    created_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date)
);
CREATE INDEX IF NOT EXISTS idx_disclosure_actual ON fin.disclosure_date (actual_date);
COMMENT ON TABLE fin.disclosure_date IS '财报披露日期(Tushare官方6字段1:1)';
"""

# ---------- Financial sync meta table ----------
DDL_FIN_SYNC_META = """
CREATE TABLE IF NOT EXISTS fin.sync_meta (
    table_name   VARCHAR(32) PRIMARY KEY,
    last_code    VARCHAR(16),                -- latest ts_code synced (resume checkpoint)
    total_rows   BIGINT DEFAULT 0,
    updated_at   TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE fin.sync_meta IS '财务数据同步水位线(按股票续传)';
"""

# ---------- Main-business composition (fina_mainbz, 1:1 with Tushare's official 9 fields, 2026-10 official output parameter table) ----------
DDL_FINA_MAINBZ = """
CREATE TABLE IF NOT EXISTS fin.fina_mainbz (
    ts_code       VARCHAR(16) NOT NULL,
    end_date      DATE NOT NULL,               -- report period
    bz_item       VARCHAR(512) NOT NULL,       -- main-business item (product/region/industry name)
    bz_code       VARCHAR(16) NOT NULL,        -- main-business source dimension: P=by product, D=by region, I=by industry (S=by sales mode; legacy data also contains extra dimension codes like 455006000/F, kept as-is)
    bz_sales      NUMERIC(24,4),               -- main-business revenue (CNY)
    bz_profit     NUMERIC(24,4),               -- main-business profit (CNY)
    bz_cost       NUMERIC(24,4),               -- main-business cost (CNY)
    curr_type     VARCHAR(8),                  -- currency code (CNY)
    update_flag   VARCHAR(4),                  -- update flag (official output field; the current API never returns it, always NULL)
    created_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, bz_code, bz_item)
);
CREATE INDEX IF NOT EXISTS idx_fina_mainbz_end_date ON fin.fina_mainbz (end_date);
COMMENT ON TABLE fin.fina_mainbz IS '主营业务构成原始表 (Tushare官方9字段1:1; 公司x报告期x维度x项目, 金额单位:元; 一次调用返回全部维度, bz_code 即维度标签)';
"""

# Main-business cleaned view: drops dimension header rows (product/industry/region) and
# totals; "of which"/colon-nested sub-items are excluded from the share denominator;
# derives revenue share and gross margin
DDL_VIEW_MAIN_BIZ = """
CREATE OR REPLACE VIEW fin.v_main_biz AS
WITH cleaned AS (
    SELECT ts_code, end_date, bz_code,
           regexp_replace(bz_item, '[（(]产品[)）]$', '') AS bz_item,
           bz_sales, bz_cost, bz_profit, curr_type,
           (bz_item LIKE '其中%' OR bz_item LIKE '%:%' OR bz_item LIKE '%：%')
               AS is_sub_item
    FROM fin.fina_mainbz
    WHERE bz_sales IS NOT NULL AND bz_sales <> 0
      AND bz_item NOT IN ('产品', '行业', '地区')
      AND bz_item NOT LIKE '%合计%'
      AND bz_item NOT LIKE '%小计%'
      AND bz_item <> '-'
),
denom AS (
    SELECT ts_code, end_date, bz_code, SUM(bz_sales) AS group_sales
    FROM cleaned
    WHERE NOT is_sub_item AND bz_sales > 0
    GROUP BY ts_code, end_date, bz_code
)
SELECT c.ts_code, c.end_date, c.bz_code, c.bz_item,
       c.bz_sales,
       c.bz_cost,
       c.bz_profit,
       ROUND(c.bz_sales / NULLIF(d.group_sales, 0) * 100, 2) AS sales_share_pct,
       CASE WHEN c.bz_sales > 0 AND c.bz_cost IS NOT NULL
            THEN ROUND((c.bz_sales - c.bz_cost) / c.bz_sales * 100, 2) END AS gross_margin_pct,
       c.curr_type,
       c.is_sub_item
FROM cleaned c
LEFT JOIN denom d USING (ts_code, end_date, bz_code);
"""

# ---------- cninfo announcements ----------
# Since 2026-10 tools/cninfo is a self-contained module (zero project deps); metadata/
# progress/orgId mapping moved to file artifacts under output/cninfo/meta/
# (tools/cninfo/store.py) — nothing is written to the DB anymore.
# The legacy tables fin.cninfo_announcement / fin.cninfo_sync_state /
# fin.cninfo_org_map keep historical data (unmigrated), for historical queries only.





FIN_ALL_DDL = [
    DDL_FIN_SCHEMA,
    DDL_INCOME,
    DDL_BALANCESHEET,
    DDL_CASHFLOW,
    DDL_FINA_INDICATOR,
    DDL_DIVIDEND,
    DDL_FINA_AUDIT,
    DDL_DISCLOSURE_DATE,
    DDL_FINA_MAINBZ,
    DDL_VIEW_MAIN_BIZ,
    DDL_FIN_SYNC_META,
]

# Financial table name constants
T_INCOME = "fin.income"
T_BALANCESHEET = "fin.balancesheet"
T_CASHFLOW = "fin.cashflow"
T_FINA_INDICATOR = "fin.fina_indicator"
T_DIVIDEND = "fin.dividend"
T_FINA_AUDIT = "fin.fina_audit"
T_DISCLOSURE_DATE = "fin.disclosure_date"
T_FINA_MAINBZ = "fin.fina_mainbz"
T_FIN_SYNC_META = "fin.sync_meta"

# The DDL for chain-graph table fin.chain_extract has moved to beta_alpha/schema.py
# (module-local ensure functions; the beta_alpha subsystem is self-governing)


def init_fin_schema(pg_client) -> None:
    """Run the financial table-creation DDL"""
    for ddl in FIN_ALL_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()


# ============================================================
