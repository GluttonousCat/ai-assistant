"""
行情数据仓库表结构 (PostgreSQL)

schema: stock
包含:
- stock_basic:   股票基础信息 (唯一股票池, 含复权因子)
- trade_calendar: 交易日历
- daily:          日K主表 (OHLCV, 前后复权价格预计算列)
- adj_factor:     复权因子明细
- daily_basic:    每日估值指标 (PE/PB/换手/市值)
- sync_meta:      同步水位线 (断点续传)
"""

# 支持的数据类型常量
DDL_STOCK_SCHEMA = """CREATE SCHEMA IF NOT EXISTS stock"""

DDL_STOCK_BASIC = """
CREATE TABLE IF NOT EXISTS stock.stock_basic (
    ts_code      VARCHAR(16) PRIMARY KEY,   -- 代码 000001.SZ
    symbol       VARCHAR(8)  NOT NULL,      -- 6位数字代码
    name         VARCHAR(32),               -- 名称
    area         VARCHAR(16),               -- 所在地域
    industry     VARCHAR(32),               -- 所属行业
    market       VARCHAR(8),                -- 市场: 主板/创业板/科创板
    list_date    DATE,                      -- 上市日期
    delist_date  DATE,                      -- 退市日期(空为在市)
    is_hs        VARCHAR(4),                -- 是否沪深港通标的
    status       VARCHAR(4),                -- L上市/D退市/P暂停
    industry_l1  VARCHAR(32),               -- 申万一级行业 (当前, 冗余快查)
    industry_l2  VARCHAR(32),               -- 申万二级行业 (当前, 冗余快查)
    updated_at   TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE stock.stock_basic IS '股票基础信息(股票池)';
"""

DDL_TRADE_CALENDAR = """
CREATE TABLE IF NOT EXISTS stock.trade_calendar (
    exchange    VARCHAR(8)  NOT NULL,   -- 交易所 SSE/SZSE
    cal_date    DATE        NOT NULL,   -- 日历日期
    is_open     SMALLINT,               -- 是否交易 1/0
    pretrade_date DATE,                 -- 前一交易日
    PRIMARY KEY (exchange, cal_date)
);
COMMENT ON TABLE stock.trade_calendar IS '交易日历';
"""

DDL_DAILY = """
CREATE TABLE IF NOT EXISTS stock.daily (
    trade_date  DATE    NOT NULL,
    ts_code     VARCHAR(16) NOT NULL,
    open        NUMERIC(12,3),
    high        NUMERIC(12,3),
    low         NUMERIC(12,3),
    close       NUMERIC(12,3),
    pre_close   NUMERIC(12,3),      -- 昨收(未复权)
    change      NUMERIC(12,3),      -- 涨跌额
    pct_chg     NUMERIC(8,3),       -- 涨跌幅%
    vol         NUMERIC(18,2),      -- 成交量(手)
    amount      NUMERIC(20,2),      -- 成交额(千元)
    adj_factor  NUMERIC(12,5),      -- 复权因子
    close_adj   NUMERIC(20,5),      -- 前复权收盘
    high_adj    NUMERIC(20,5),      -- 前复权最高
    low_adj     NUMERIC(20,5),      -- 前复权最低
    open_adj    NUMERIC(20,5),      -- 前复权开盘
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_daily_ts_code ON stock.daily (ts_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_trade_date ON stock.daily (trade_date);
COMMENT ON TABLE stock.daily IS '日K主表(含前复权价格)';
"""

DDL_ADJ_FACTOR = """
CREATE TABLE IF NOT EXISTS stock.adj_factor (
    ts_code     VARCHAR(16) NOT NULL,
    trade_date  DATE    NOT NULL,
    adj_factor  NUMERIC(12,5) NOT NULL,
    PRIMARY KEY (ts_code, trade_date)
);
COMMENT ON TABLE stock.adj_factor IS '复权因子明细(后复权因子)';
"""

DDL_DAILY_BASIC = """
CREATE TABLE IF NOT EXISTS stock.daily_basic (
    trade_date  DATE    NOT NULL,
    ts_code     VARCHAR(16) NOT NULL,
    close       NUMERIC(12,3),       -- 收盘价
    turnover_rate   NUMERIC(12,4),   -- 换手率%
    turnover_rate_f NUMERIC(12,4),   -- 换手率(自由流通)
    volume_ratio    NUMERIC(12,4),   -- 量比
    pe          NUMERIC(16,4),       -- 市盈率(TTM)
    pe_ttm      NUMERIC(16,4),       -- 市盈率(TTM)
    pb          NUMERIC(16,4),       -- 市净率
    ps          NUMERIC(16,4),       -- 市销率TTM
    ps_ttm      NUMERIC(16,4),
    dv_ratio    NUMERIC(12,4),       -- 股息率%
    dv_ttm      NUMERIC(12,4),
    total_share NUMERIC(20,4),       -- 总股本(万股)
    float_share NUMERIC(20,4),       -- 流通股本(万股)
    free_share  NUMERIC(20,4),       -- 自由流通股本(万股)
    total_mv    NUMERIC(20,4),       -- 总市值(万元)
    circ_mv     NUMERIC(20,4),       -- 流通市值(万元)
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_daily_basic_ts_code ON stock.daily_basic (ts_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_basic_trade_date ON stock.daily_basic (trade_date);
COMMENT ON TABLE stock.daily_basic IS '每日估值与市场指标';
"""

DDL_SYNC_META = """
CREATE TABLE IF NOT EXISTS stock.sync_meta (
    table_name     VARCHAR(32) PRIMARY KEY,   -- daily / adj_factor / daily_basic
    latest_date    DATE,                      -- 已同步到的最近交易日
    total_rows     BIGINT DEFAULT 0,          -- 累计已同步行数
    updated_at     TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE stock.sync_meta IS '数据同步水位线(断点续传用)';
"""

# 建表顺序 (依赖关系: stock_basic 无依赖, daily_basic 独立, 均可并行)
ALL_DDL = [
    DDL_STOCK_SCHEMA,
    DDL_STOCK_BASIC,
    DDL_TRADE_CALENDAR,
    DDL_DAILY,
    DDL_ADJ_FACTOR,
    DDL_DAILY_BASIC,
    DDL_SYNC_META,
]

# 表名常量
T_STOCK_BASIC = "stock.stock_basic"
T_TRADE_CALENDAR = "stock.trade_calendar"
T_DAILY = "stock.daily"
T_ADJ_FACTOR = "stock.adj_factor"
T_DAILY_BASIC = "stock.daily_basic"
T_SYNC_META = "stock.sync_meta"


def init_schema(pg_client) -> None:
    """执行全部建表 DDL"""
    for ddl in ALL_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()

# ============================================================
# 财务数据仓库 (schema: fin)
# ============================================================
# 四大财务报表 + 财务指标, 全字段覆盖 Tushare 规范

DDL_FIN_SCHEMA = """CREATE SCHEMA IF NOT EXISTS fin"""

# ---------- 利润表 ----------
DDL_INCOME = """
CREATE TABLE IF NOT EXISTS fin.income (
    ts_code          VARCHAR(16) NOT NULL,   -- TS代码
    ann_date         DATE,                   -- 公告日期
    f_ann_date       DATE,                   -- 实际公告日期
    end_date         DATE NOT NULL,          -- 报告期
    report_type      VARCHAR(2) NOT NULL DEFAULT '1',  -- 报告类型 1合并/6母公司/4调整合并
    comp_type        VARCHAR(2),             -- 公司类型 1工商/2银行/3保险/4证券
    end_type         VARCHAR(2),             -- 报告期类型
    basic_eps        NUMERIC(20,6),          -- 基本每股收益
    diluted_eps      NUMERIC(20,6),          -- 稀释每股收益
    total_revenue    NUMERIC(24,4),          -- 营业总收入
    revenue          NUMERIC(24,4),          -- 营业收入
    int_income       NUMERIC(24,4),          -- 利息收入
    prem_earned      NUMERIC(24,4),          -- 已赚保费
    comm_income      NUMERIC(24,4),          -- 手续费及佣金收入
    n_commis_income  NUMERIC(24,4),          -- 手续费及佣金净收入
    n_oth_income     NUMERIC(24,4),          -- 其他经营净收益
    n_oth_b_income   NUMERIC(24,4),          -- 加:其他业务净收益
    prem_income      NUMERIC(24,4),          -- 保险业务收入
    out_prem         NUMERIC(24,4),          -- 减:分出保费
    une_prem_reser   NUMERIC(24,4),          -- 提取未到期责任准备金
    reins_income     NUMERIC(24,4),          -- 其中:分保费收入
    n_sec_tb_income  NUMERIC(24,4),          -- 代理买卖证券业务净收入
    n_sec_uw_income  NUMERIC(24,4),          -- 证券承销业务净收入
    n_asset_mg_income NUMERIC(24,4),         -- 受托客户资产管理业务净收入
    oth_b_income     NUMERIC(24,4),          -- 其他业务收入
    fv_value_chg_gain NUMERIC(24,4),         -- 加:公允价值变动净收益
    invest_income    NUMERIC(24,4),          -- 加:投资净收益
    ass_invest_income NUMERIC(24,4),         -- 其中:对联营企业和合营企业的投资收益
    forex_gain       NUMERIC(24,4),          -- 加:汇兑净收益
    total_cogs       NUMERIC(24,4),          -- 营业总成本
    oper_cost        NUMERIC(24,4),          -- 减:营业成本
    int_exp          NUMERIC(24,4),          -- 减:利息支出
    comm_exp         NUMERIC(24,4),          -- 减:手续费及佣金支出
    biz_tax_surchg   NUMERIC(24,4),          -- 减:营业税金及附加
    sell_exp         NUMERIC(24,4),          -- 减:销售费用
    admin_exp        NUMERIC(24,4),          -- 减:管理费用
    fin_exp          NUMERIC(24,4),          -- 减:财务费用
    assets_impair_loss NUMERIC(24,4),        -- 减:资产减值损失
    prem_refund      NUMERIC(24,4),          -- 退保金
    compens_payout   NUMERIC(24,4),          -- 赔付总支出
    reser_insur_liab NUMERIC(24,4),          -- 提取保险责任准备金
    div_payt         NUMERIC(24,4),          -- 保户红利支出
    reins_exp        NUMERIC(24,4),          -- 分保费用
    oper_exp         NUMERIC(24,4),          -- 营业支出
    compens_payout_refu NUMERIC(24,4),       -- 减:摊回赔付支出
    insur_reser_refu NUMERIC(24,4),          -- 减:摊回保险责任准备金
    reins_cost_refund NUMERIC(24,4),         -- 减:摊回分保费用
    other_bus_cost   NUMERIC(24,4),          -- 其他业务成本
    operate_profit   NUMERIC(24,4),          -- 营业利润
    non_oper_income  NUMERIC(24,4),          -- 加:营业外收入
    non_oper_exp     NUMERIC(24,4),          -- 减:营业外支出
    nca_disploss     NUMERIC(24,4),          -- 其中:减:非流动资产处置净损失
    total_profit     NUMERIC(24,4),          -- 利润总额
    income_tax       NUMERIC(24,4),          -- 所得税费用
    n_income         NUMERIC(24,4),          -- 净利润(含少数股东损益)
    n_income_attr_p  NUMERIC(24,4),          -- 净利润(不含少数股东损益)
    minority_gain    NUMERIC(24,4),          -- 少数股东损益
    oth_compr_income NUMERIC(24,4),          -- 其他综合收益
    t_compr_income   NUMERIC(24,4),          -- 综合收益总额
    compr_inc_attr_p NUMERIC(24,4),          -- 归属于母公司综合收益总额
    compr_inc_attr_m_s NUMERIC(24,4),        -- 归属于少数股东综合收益总额
    ebit             NUMERIC(24,4),          -- 息税前利润
    ebitda           NUMERIC(24,4),          -- 息税折旧摊销前利润
    insurance_exp    NUMERIC(24,4),          -- 保险业务支出
    undist_profit    NUMERIC(24,4),          -- 年初未分配利润
    distable_profit  NUMERIC(24,4),          -- 可分配利润
    rd_exp           NUMERIC(24,4),          -- 研发费用
    fin_exp_int_exp  NUMERIC(24,4),          -- 财务费用:利息费用
    fin_exp_int_inc  NUMERIC(24,4),          -- 财务费用:利息收入
    transfer_surplus_rese NUMERIC(24,4),     -- 盈余公积转入
    transfer_housing_imprest NUMERIC(24,4),  -- 住房周转金转入
    transfer_oth     NUMERIC(24,4),          -- 其他转入
    adj_lossgain     NUMERIC(24,4),          -- 调整以前年度损益
    withdra_legal_surplus NUMERIC(24,4),     -- 提取法定盈余公积
    withdra_legal_pubfund NUMERIC(24,4),     -- 提取法定公益金
    withdra_biz_devfund NUMERIC(24,4),       -- 提取企业发展基金
    withdra_rese_fund NUMERIC(24,4),         -- 提取储备基金
    withdra_oth_ersu NUMERIC(24,4),          -- 提取任意盈余公积金
    workers_welfare  NUMERIC(24,4),          -- 职工奖金福利
    distr_profit_shrhder NUMERIC(24,4),      -- 可供股东分配的利润
    prfshare_payable_dvd NUMERIC(24,4),      -- 应付优先股股利
    comshare_payable_dvd NUMERIC(24,4),      -- 应付普通股股利
    capit_comstock_div NUMERIC(24,4),        -- 转作股本的普通股股利
    net_after_nr_lp_correct NUMERIC(24,4),   -- 扣除非经常性损益后的净利润(更正前)
    credit_impa_loss NUMERIC(24,4),          -- 信用减值损失
    net_expo_hedging_benefits NUMERIC(24,4), -- 净敞口套期收益
    oth_impair_loss_assets NUMERIC(24,4),    -- 其他资产减值损失
    total_opcost     NUMERIC(24,4),          -- 营业总成本(二)
    amodcost_fin_assets NUMERIC(24,4),       -- 以摊余成本计量的金融资产终止确认收益
    oth_income       NUMERIC(24,4),          -- 其他收益
    asset_disp_income NUMERIC(24,4),         -- 资产处置收益
    continued_net_profit NUMERIC(24,4),      -- 持续经营净利润
    end_net_profit   NUMERIC(24,4),          -- 终止经营净利润
    update_flag      VARCHAR(4),             -- 更新标识
    created_at       TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.income IS '利润表(全字段)';
"""


# ---------- 资产负债表 ----------
DDL_BALANCESHEET = """
CREATE TABLE IF NOT EXISTS fin.balancesheet (
    ts_code          VARCHAR(16) NOT NULL,
    ann_date         DATE,
    f_ann_date       DATE,
    end_date         DATE NOT NULL,
    report_type      VARCHAR(2) NOT NULL DEFAULT '1',
    comp_type        VARCHAR(2),
    end_type         VARCHAR(2),
    total_share      NUMERIC(24,4),          -- 总股本
    cap_rese         NUMERIC(24,4),          -- 资本公积金
    undistr_porfit   NUMERIC(24,4),          -- 未分配利润
    surplus_rese     NUMERIC(24,4),          -- 盈余公积金
    special_rese     NUMERIC(24,4),          -- 专项储备
    money_cap        NUMERIC(24,4),          -- 货币资金
    trad_asset       NUMERIC(24,4),          -- 交易性金融资产
    notes_receiv     NUMERIC(24,4),          -- 应收票据
    accounts_receiv  NUMERIC(24,4),          -- 应收账款
    oth_receiv       NUMERIC(24,4),          -- 其他应收款
    prepayment       NUMERIC(24,4),          -- 预付款项
    invent           NUMERIC(24,4),          -- 存货
    oth_cur_assets   NUMERIC(24,4),          -- 其他流动资产
    fix_assets       NUMERIC(24,4),          -- 固定资产
    cip              NUMERIC(24,4),          -- 在建工程
    intang_assets    NUMERIC(24,4),          -- 无形资产
    r_and_d          NUMERIC(24,4),          -- 研发费用(资本化)
    goodwill         NUMERIC(24,4),          -- 商誉
    lt_equity_invest NUMERIC(24,4),          -- 长期股权投资
    oth_eq_invest    NUMERIC(24,4),          -- 其他权益工具投资
    oth_lt_invest    NUMERIC(24,4),          -- 其他长期投资
    lt_rec            NUMERIC(24,4),         -- 长期应收款
    dep_inv          NUMERIC(24,4),          -- 投资性房地产
    oth_ncur_assets  NUMERIC(24,4),          -- 其他非流动资产
    total_assets     NUMERIC(24,4),          -- 资产总计
    st_borr          NUMERIC(24,4),          -- 短期借款
    notes_payable    NUMERIC(24,4),          -- 应付票据
    acct_payable     NUMERIC(24,4),          -- 应付账款
    adv_receipts     NUMERIC(24,4),          -- 预收款项
    employee_payable NUMERIC(24,4),          -- 应付职工薪酬
    taxes_payable    NUMERIC(24,4),          -- 应交税费
    oth_cur_liab     NUMERIC(24,4),          -- 其他流动负债
    bond_payable     NUMERIC(24,4),          -- 应付债券
    lt_payable       NUMERIC(24,4),          -- 长期应付款
    lt_borr          NUMERIC(24,4),          -- 长期借款
    total_liab       NUMERIC(24,4),          -- 负债合计
    equity_attr_p    NUMERIC(24,4),          -- 归属于母公司股东权益
    minority_int     NUMERIC(24,4),          -- 少数股东权益
    total_hldr_eqy_exc_min_int NUMERIC(24,4), -- 股东权益合计(不含少数股东)
    total_hldr_eqy_inc_min_int NUMERIC(24,4), -- 股东权益合计(含少数股东)
    total_liab_hldr_eqy NUMERIC(24,4),       -- 负债和股东权益总计
    -- 补充关键衍生字段
    nca_disploss     NUMERIC(24,4),          -- 处置非流动资产损失
    credit_impa_loss NUMERIC(24,4),          -- 信用减值损失
    update_flag      VARCHAR(4),
    created_at       TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.balancesheet IS '资产负债表(全字段)';
"""

# ---------- 现金流量表 ----------
DDL_CASHFLOW = """
CREATE TABLE IF NOT EXISTS fin.cashflow (
    ts_code          VARCHAR(16) NOT NULL,
    ann_date         DATE,
    f_ann_date       DATE,
    end_date         DATE NOT NULL,
    report_type      VARCHAR(2) NOT NULL DEFAULT '1',
    comp_type        VARCHAR(2),
    end_type         VARCHAR(2),
    -- 经营活动现金流
    n_cashflow_act   NUMERIC(24,4),          -- 经营活动产生的现金流量净额
    n_cashflow_inv_act NUMERIC(24,4),        -- 投资活动产生的现金流量净额
    n_cash_flows_fnc_act NUMERIC(24,4),      -- 筹资活动产生的现金流量净额
    c_fr_sale_sg     NUMERIC(24,4),          -- 销售商品、提供劳务收到的现金
    recp_tax_rends   NUMERIC(24,4),          -- 收到的税费返还
    n_depos_incr_fi  NUMERIC(24,4),          -- 客户存款和同业存放款项净增加额
    n_incr_loans_cb  NUMERIC(24,4),          -- 向中央银行借款净增加额
    n_inc_borr_oth_fi NUMERIC(24,4),        -- 向其他金融机构借款净增加额
    n_incr_disp_fiolig NUMERIC(24,4),        -- 处置固定资产等活动净增加额
    c_paid_gs_for_rtr NUMERIC(24,4),        -- 购买商品、接受劳务支付的现金
    n_incr_disp_fiolis NUMERIC(24,4),        -- 处置固定资产、无形资产净增加额
    c_paid_for_intang_invest NUMERIC(24,4),  -- 购建固定资产等活动支付的现金
    -- 实际字段以 Tushare 返回为准, 核心三大净额已覆盖
    -- (完整字段过多, 采用通用列并兼容)
    n_cashflow_act_pre NUMERIC(24,4),        -- 经营活动现金流量净额(上一期)
    n_invest_income_subsidiary NUMERIC(24,4),-- 取得子公司投资支付
    free_cashflow    NUMERIC(24,4),          -- 自由现金流(派生)
    update_flag      VARCHAR(4),
    created_at       TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.cashflow IS '现金流量表(核心字段,按需扩展)';
"""

# ---------- 财务指标 ----------
DDL_FINA_INDICATOR = """
CREATE TABLE IF NOT EXISTS fin.fina_indicator (
    ts_code          VARCHAR(16) NOT NULL,
    ann_date         DATE,
    end_date         DATE NOT NULL,
    report_type      VARCHAR(2) NOT NULL DEFAULT '1',
    comp_type        VARCHAR(2),
    roe              NUMERIC(16,6),          -- 净资产收益率
    roe_waa          NUMERIC(16,6),          -- 加权平均净资产收益率
    roe_dt           NUMERIC(16,6),          -- 净资产收益率(扣除/摊薄)
    roa              NUMERIC(16,6),          -- 总资产报酬率
    r_roe             NUMERIC(16,6),
    netprofit_yoy    NUMERIC(16,6),          -- 净利润同比增长率
    or_yoy           NUMERIC(16,6),          -- 营业收入同比增长率
    ope_or_yoy       NUMERIC(16,6),          -- 营业利润同比增长率
    dt_netprofit_yoy NUMERIC(16,6),          -- 扣非净利润同比增长率
    grossprofit_margin NUMERIC(16,6),        -- 销售毛利率
    netprofit_margin NUMERIC(16,6),          -- 销售净利率
    ocfps           NUMERIC(16,6),           -- 每股经营现金流
    eps             NUMERIC(16,6),           -- 每股收益
    bps             NUMERIC(16,6),           -- 每股净资产
    assets_turn     NUMERIC(16,6),           -- 总资产周转率
    inv_turn        NUMERIC(16,6),           -- 存货周转率
    ca_turn         NUMERIC(16,6),           -- 应收账款周转率
    debt_to_assets  NUMERIC(16,6),           -- 资产负债率
    current_ratio   NUMERIC(16,6),           -- 流动比率
    quick_ratio     NUMERIC(16,6),           -- 速动比率
    update_flag     VARCHAR(4),
    created_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.fina_indicator IS '财务指标(ROE/毛利率/周转率/偿债能力)';
"""

# ---------- 财务同步元表 ----------
DDL_FIN_SYNC_META = """
CREATE TABLE IF NOT EXISTS fin.sync_meta (
    table_name   VARCHAR(32) PRIMARY KEY,
    last_code    VARCHAR(16),                -- 已同步到的最近 ts_code (断点续传)
    total_rows   BIGINT DEFAULT 0,
    updated_at   TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE fin.sync_meta IS '财务数据同步水位线(按股票续传)';
"""

FIN_ALL_DDL = [
    DDL_FIN_SCHEMA,
    DDL_INCOME,
    DDL_BALANCESHEET,
    DDL_CASHFLOW,
    DDL_FINA_INDICATOR,
    DDL_FIN_SYNC_META,
]

# 财务表名常量
T_INCOME = "fin.income"
T_BALANCESHEET = "fin.balancesheet"
T_CASHFLOW = "fin.cashflow"
T_FINA_INDICATOR = "fin.fina_indicator"
T_FIN_SYNC_META = "fin.sync_meta"


def init_fin_schema(pg_client) -> None:
    """执行财务建表 DDL"""
    for ddl in FIN_ALL_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()


# ============================================================
# 宽表视图 (简化 Agent/LLM 查询)
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
    b.equity_attr_p,
    b.money_cap,
    b.accounts_receiv,
    b.invent,
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
    """部署宽表视图 (幂等)"""
    for ddl in VIEW_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()


# ============================================================
# 股票别名词典 (简称/外号 -> ts_code)
# ============================================================

DDL_STOCK_ALIAS = """
CREATE TABLE IF NOT EXISTS stock.stock_alias (
    alias       VARCHAR(32) PRIMARY KEY,   -- 别名 (简称/外号)
    ts_code     VARCHAR(16) NOT NULL,      -- 标准代码
    alias_type  VARCHAR(8) DEFAULT 'nick', -- nickname/abbr/cypher
    created_at  TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stock_alias_ts ON stock.stock_alias (ts_code);
COMMENT ON TABLE stock.stock_alias IS '股票别名词典(简称/外号/谐音)';
"""

# 种子别名 (初始覆盖常见简称, 后续可人工扩充)
SEED_ALIASES: dict = {
    # 银行
    "平安银行": "000001.SZ", "平银": "000001.SZ",
    "招商银行": "600036.SH", "招行": "600036.SH",
    "工商银行": "601398.SH", "工行": "601398.SH",
    "建设银行": "601939.SH", "建行": "601939.SH",
    "农业银行": "601288.SH", "农行": "601288.SH",
    "中国银行": "601988.SH", "中行": "601988.SH",
    "交通银行": "601328.SH", "交行": "601328.SH",
    "浦发银行": "600000.SH", "浦发": "600000.SH",
    "兴业银行": "601166.SH", "兴业": "601166.SH",
    "民生银行": "600016.SH", "民生": "600016.SH",
    "宁波银行": "002142.SZ", "宁波": "002142.SZ",
    # 白酒
    "贵州茅台": "600519.SH", "茅台": "600519.SH", "毛子": "600519.SH", "贵茅": "600519.SH",
    "五粮液": "000858.SZ", "五粮": "000858.SZ",
    "泸州老窖": "000568.SZ", "泸州": "000568.SZ", "泸老窖": "000568.SZ",
    "洋河股份": "002304.SZ", "洋河": "002304.SZ",
    "山西汾酒": "600809.SH", "汾酒": "600809.SH",
    "古井贡酒": "000596.SZ", "古井": "000596.SZ",
    # 科技/半导体
    "宁德时代": "300750.SZ", "宁德": "300750.SZ", "曾毓群家": "300750.SZ",
    "比亚迪": "002594.SZ", "比王": "002594.SZ", "BYD": "002594.SZ",
    "中芯国际": "688981.SH", "中芯": "688981.SH",
    "北方华创": "002371.SZ", "北华": "002371.SZ",
    "韦尔股份": "603501.SH", "韦尔": "603501.SH",
    "兆易创新": "603986.SH", "兆易": "603986.SH", "赵毅": "603986.SH", "赵姨": "603986.SH",
    "汇顶科技": "603160.SH", "汇顶": "603160.SH",
    "卓胜微": "300782.SZ", "卓胜": "300782.SZ",
    "圣邦股份": "300661.SZ", "圣邦": "300661.SZ",
    # 消费电子/PCB
    "立讯精密": "002475.SZ", "立讯": "002475.SZ",
    "歌尔股份": "002241.SZ", "歌尔": "002241.SZ",
    "京东方A": "000725.SZ", "京东方": "000725.SZ", "京东方A": "000725.SZ",
    "深南电路": "002916.SZ", "深南": "002916.SZ",
    "沪电股份": "002463.SZ", "沪电": "002463.SZ",
    "生益科技": "600183.SH", "生益": "600183.SH",
    "胜宏科技": "300476.SZ", "胜宏": "300476.SZ",
    "鹏鼎控股": "002938.SZ", "鹏鼎": "002938.SZ",
    # 新能源
    "隆基绿能": "601012.SH", "隆基": "601012.SH",
    "通威股份": "600438.SH", "通威": "600438.SH",
    "阳光电源": "300274.SZ", "阳光": "300274.SZ",
    "亿纬锂能": "300014.SZ", "亿纬": "300014.SZ",
    "赣锋锂业": "002460.SZ", "赣锋": "002460.SZ",
    "天齐锂业": "002466.SZ", "天齐": "002466.SZ",
    # 医药
    "药明康德": "603259.SH", "药明": "603259.SH",
    "恒瑞医药": "600276.SH", "恒瑞": "600276.SH",
    "迈瑞医疗": "300760.SZ", "迈瑞": "300760.SZ",
    "爱尔眼科": "300015.SZ", "爱尔": "300015.SZ",
    "片仔癀": "600436.SH", "片仔癀": "600436.SH",
    # 家电/消费
    "美的集团": "000333.SZ", "美的": "000333.SZ",
    "格力电器": "000651.SZ", "格力": "000651.SZ",
    "海尔智家": "600690.SH", "海尔": "600690.SH",
    # 互联网/券商
    "东方财富": "300059.SZ", "东财": "300059.SZ", "东方财富": "300059.SZ",
    "同花顺": "300033.SZ",
    "中信证券": "600030.SH", "中信": "600030.SH",
    "中国平安": "601318.SH", "平安": "601318.SH", "平保": "601318.SH",
    "万科A": "000002.SZ", "万科": "000002.SZ",
    "深振业A": "000006.SZ", "深振业": "000006.SZ",
}

# ============================================================
# 研报数据仓库 (schema: fin) — PRD 2.2
# ============================================================
# 数据来源: 知识星球爬虫 (话题附件: PDF/DOCX/TXT) + 手动上传
# 用途: F4 研报一致性校验 / F5 分析报告生成 的数据底座

DDL_REPORT_META = """
CREATE TABLE IF NOT EXISTS fin.report_meta (
    report_id       SERIAL PRIMARY KEY,
    topic_id        BIGINT,                    -- 知识星球话题ID (来源标识)
    file_id         BIGINT,                    -- 知识星球文件ID
    ts_code         VARCHAR(16),               -- 标的股票 (文本推断, 可空)
    title           VARCHAR(256),              -- 文件标题
    author          VARCHAR(64),               -- 分析师
    org_name        VARCHAR(64),               -- 券商名称
    publish_date    DATE,                      -- 发布日期
    report_type     VARCHAR(16),               -- 研报类型 (深度/点评/季报)
    source          VARCHAR(32) DEFAULT 'zsxq',-- 来源 (zsxq/upload)
    file_path       TEXT,                      -- 本地文件路径
    file_name       VARCHAR(256),              -- 原始文件名
    file_size       BIGINT,                    -- 文件大小(字节)
    content_text    TEXT,                      -- 提取的文本内容
    content_chars   INTEGER DEFAULT 0,         -- 文本长度 (质量检查)
    extraction_status VARCHAR(16) DEFAULT 'pending', -- pending/extracted/failed
    created_at      TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_report_meta_ts_code ON fin.report_meta (ts_code);
CREATE INDEX IF NOT EXISTS idx_report_meta_topic ON fin.report_meta (topic_id);
COMMENT ON TABLE fin.report_meta IS '研报元数据与文本内容';
"""

DDL_REPORT_FORECAST = """
CREATE TABLE IF NOT EXISTS fin.report_forecast (
    id              SERIAL PRIMARY KEY,
    report_id       INTEGER NOT NULL REFERENCES fin.report_meta(report_id),
    ts_code         VARCHAR(16) NOT NULL,
    forecast_type   VARCHAR(32),               -- 预测类型: revenue/net_profit/eps/target_price
    forecast_period VARCHAR(16),               -- 预测期间: 2024/2025E
    forecast_value  NUMERIC(24,4),             -- 预测值
    forecast_unit   VARCHAR(16),               -- 单位: 亿元/元
    confidence      NUMERIC(5,2),              -- 提取置信度 0-100
    raw_text        TEXT,                      -- 原始文本片段
    created_at      TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_report_forecast_report ON fin.report_forecast (report_id);
COMMENT ON TABLE fin.report_forecast IS '研报关键预测数据提取结果';
"""

DDL_REPORT_SYNC_META = """
CREATE TABLE IF NOT EXISTS fin.report_sync_meta (
    key_name     VARCHAR(32) PRIMARY KEY,      -- last_topic_id / last_file_id
    value_str    VARCHAR(64),
    value_int    BIGINT,
    updated_at   TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE fin.report_sync_meta IS '研报同步水位线(zsxq→PG 断点续传)';
"""

REPORT_DDL = [
    DDL_REPORT_META,
    DDL_REPORT_FORECAST,
    DDL_REPORT_SYNC_META,
]

T_REPORT_META = "fin.report_meta"
T_REPORT_FORECAST = "fin.report_forecast"
T_REPORT_SYNC_META = "fin.report_sync_meta"


def init_report_schema(pg_client) -> None:
    """部署研报相关表 (幂等)"""
    for ddl in REPORT_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()


DDL_VIEW_REPORT_FILES = """
CREATE OR REPLACE VIEW fin.v_report_ready AS
SELECT
    r.report_id,
    r.ts_code,
    r.title,
    r.file_name,
    r.file_size,
    r.file_path,
    r.report_type,
    r.source,
    r.extraction_status,
    r.content_chars,
    r.publish_date,
    r.created_at
FROM fin.report_meta r
WHERE r.extraction_status = 'extracted'
  AND r.ts_code IS NOT NULL
  AND r.content_chars > 0;
COMMENT ON VIEW fin.v_report_ready IS '已提取且含标的股票的研报(可直接用于F4校验)';
"""

REPORT_VIEW_DDL = [DDL_VIEW_REPORT_FILES]


def init_report_views(pg_client) -> None:
    """部署研报视图 (幂等)"""
    for ddl in REPORT_VIEW_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()


# ============================================================
# 申万行业分类 (SW2021) + 个股行业映射
# ============================================================

DDL_INDEX_CLASSIFY = """
CREATE TABLE IF NOT EXISTS stock.index_classify (
    index_code     VARCHAR(16) PRIMARY KEY,  -- 行业指数代码 (801125.SI)
    industry_name  VARCHAR(32) NOT NULL,     -- 行业名称 (白酒Ⅱ)
    level          VARCHAR(4),               -- L1/L2/L3
    industry_code  VARCHAR(16),              -- 行业代码 (340500)
    is_pub         SMALLINT,                 -- 是否公开行业
    parent_code    VARCHAR(16),              -- 父级行业代码 (340000)
    src            VARCHAR(16),              -- 来源 (SW2021)
    upd_date       VARCHAR(16)               -- 更新日期
);
COMMENT ON TABLE stock.index_classify IS '申万行业分类 (SW2021)';
"""

DDL_STOCK_INDUSTRY = """
CREATE TABLE IF NOT EXISTS stock.stock_industry (
    index_code    VARCHAR(16) NOT NULL,   -- 二级行业指数代码
    con_code      VARCHAR(16) NOT NULL,   -- 成分股 ts_code
    in_date       VARCHAR(16),            -- 调入日期 (YYYYMMDD)
    out_date      VARCHAR(16),            -- 调出日期 (空=仍在)
    is_new        VARCHAR(2),             -- N=否 Y=是(新调入)
    PRIMARY KEY (index_code, con_code)
);
CREATE INDEX IF NOT EXISTS idx_stock_ind_con ON stock.stock_industry (con_code);
COMMENT ON TABLE stock.stock_industry IS '个股 -> 申万二级行业映射';
"""


# ============================================================
# (行北行业 DDL 已在上面: DDL_INDEX_CLASSIFY + DDL_STOCK_INDUSTRY)
# ============================================================
