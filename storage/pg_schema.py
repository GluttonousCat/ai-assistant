import logging
from contextlib import contextmanager

import psycopg2

from storage.pg import pg_config

logger = logging.getLogger(__name__)

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
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_daily_ts_code ON stock.daily (ts_code, trade_date);
COMMENT ON TABLE stock.daily IS '日K主表(未复权; 复权因子在 stock.adj_factor)';
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

# ---------- 十大股东 (Tushare top10_holders; 单接口已含流通比例/变动) ----------
DDL_TOP10_HOLDERS = """
CREATE TABLE IF NOT EXISTS stock.top10_holders (
    ts_code          VARCHAR(16) NOT NULL,   -- TS代码
    ann_date         DATE,                   -- 公告日期
    end_date         DATE NOT NULL,          -- 报告期
    holder_name      VARCHAR(128) NOT NULL,  -- 股东名称
    hold_amount      NUMERIC(20,2),          -- 持股数量 (万股)
    hold_ratio       NUMERIC(10,4),          -- 持股比例 %
    hold_float_ratio NUMERIC(10,4),          -- 流通股持股比例 %
    hold_change      NUMERIC(20,2),          -- 持股变动 (万股)
    holder_type      VARCHAR(32),            -- 股东性质 (实际返回中文: 投资公司/自然人/基金等)
    updated_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, holder_name, holder_type)
);
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
    DDL_TOP10_HOLDERS,
]

# 表名常量
T_STOCK_BASIC = "stock.stock_basic"
T_TRADE_CALENDAR = "stock.trade_calendar"
T_DAILY = "stock.daily"
T_ADJ_FACTOR = "stock.adj_factor"
T_DAILY_BASIC = "stock.daily_basic"
T_SYNC_META = "stock.sync_meta"
T_TOP10_HOLDERS = "stock.top10_holders"


def init_schema(pg_client) -> None:
    """执行全部建表 DDL"""
    for ddl in ALL_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()

# ============================================================
# 财务数据仓库 (schema: fin)
# ============================================================
# 三大财务报表+财务指标+分红送股+审计意见+披露日期+主营构成与官方文档全字段 1:1
# (income 94 / balancesheet 158 / cashflow 97 / fina_indicator 167+本地report_type /
#  dividend 16 / fina_audit 7 / disclosure_date 6 / fina_mainbz 9, 2026-10 官网输出参数表;
#  存量库曾为子集+错名列, 已迁齐并删死列)

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


# ---------- 资产负债表 (Tushare 官方 158 字段 1:1, 2026-10 官网输出参数表) ----------
DDL_BALANCESHEET = """
CREATE TABLE IF NOT EXISTS fin.balancesheet (
    ts_code                     VARCHAR(16) NOT NULL,      -- TS股票代码
    ann_date                    DATE,                      -- 公告日期
    f_ann_date                  DATE,                      -- 实际公告日期
    end_date                    DATE NOT NULL,             -- 报告期
    report_type                 VARCHAR(2) NOT NULL DEFAULT '1', -- 报表类型
    comp_type                   VARCHAR(2),                -- 公司类型(1一般工商业2银行3保险4证券7多元金融)
    end_type                    VARCHAR(2),                -- 报告期类型
    total_share                 NUMERIC(24,4),             -- 期末总股本（股）
    cap_rese                    NUMERIC(24,4),             -- 资本公积金
    undistr_porfit              NUMERIC(24,4),             -- 未分配利润
    surplus_rese                NUMERIC(24,4),             -- 盈余公积金
    special_rese                NUMERIC(24,4),             -- 专项储备
    money_cap                   NUMERIC(24,4),             -- 货币资金
    trad_asset                  NUMERIC(24,4),             -- 交易性金融资产
    notes_receiv                NUMERIC(24,4),             -- 应收票据
    accounts_receiv             NUMERIC(24,4),             -- 应收账款
    oth_receiv                  NUMERIC(24,4),             -- 其他应收款
    prepayment                  NUMERIC(24,4),             -- 预付款项
    div_receiv                  NUMERIC(24,4),             -- 应收股利
    int_receiv                  NUMERIC(24,4),             -- 应收利息
    inventories                 NUMERIC(24,4),             -- 存货
    amor_exp                    NUMERIC(24,4),             -- 待摊费用
    nca_within_1y               NUMERIC(24,4),             -- 一年内到期的非流动资产
    sett_rsrv                   NUMERIC(24,4),             -- 结算备付金
    loanto_oth_bank_fi          NUMERIC(24,4),             -- 拆出资金
    premium_receiv              NUMERIC(24,4),             -- 应收保费
    reinsur_receiv              NUMERIC(24,4),             -- 应收分保账款
    reinsur_res_receiv          NUMERIC(24,4),             -- 应收分保合同准备金
    pur_resale_fa               NUMERIC(24,4),             -- 买入返售金融资产
    oth_cur_assets              NUMERIC(24,4),             -- 其他流动资产
    total_cur_assets            NUMERIC(24,4),             -- 流动资产合计
    fa_avail_for_sale           NUMERIC(24,4),             -- 可供出售金融资产
    htm_invest                  NUMERIC(24,4),             -- 持有至到期投资
    lt_eqt_invest               NUMERIC(24,4),             -- 长期股权投资
    invest_real_estate          NUMERIC(24,4),             -- 投资性房地产
    time_deposits               NUMERIC(24,4),             -- 定期存款
    oth_assets                  NUMERIC(24,4),             -- 其他资产
    lt_rec                      NUMERIC(24,4),             -- 长期应收款
    fix_assets                  NUMERIC(24,4),             -- 固定资产
    cip                         NUMERIC(24,4),             -- 在建工程
    const_materials             NUMERIC(24,4),             -- 工程物资
    fixed_assets_disp           NUMERIC(24,4),             -- 固定资产清理
    produc_bio_assets           NUMERIC(24,4),             -- 生产性生物资产
    oil_and_gas_assets          NUMERIC(24,4),             -- 油气资产
    intan_assets                NUMERIC(24,4),             -- 无形资产
    r_and_d                     NUMERIC(24,4),             -- 研发支出
    goodwill                    NUMERIC(24,4),             -- 商誉
    lt_amor_exp                 NUMERIC(24,4),             -- 长期待摊费用
    defer_tax_assets            NUMERIC(24,4),             -- 递延所得税资产
    decr_in_disbur              NUMERIC(24,4),             -- 发放贷款及垫款
    oth_nca                     NUMERIC(24,4),             -- 其他非流动资产
    total_nca                   NUMERIC(24,4),             -- 非流动资产合计
    cash_reser_cb               NUMERIC(24,4),             -- 现金及存放中央银行款项
    depos_in_oth_bfi            NUMERIC(24,4),             -- 存放同业和其它金融机构款项
    prec_metals                 NUMERIC(24,4),             -- 贵金属
    deriv_assets                NUMERIC(24,4),             -- 衍生金融资产
    rr_reins_une_prem           NUMERIC(24,4),             -- 应收分保未到期责任准备金
    rr_reins_outstd_cla         NUMERIC(24,4),             -- 应收分保未决赔款准备金
    rr_reins_lins_liab          NUMERIC(24,4),             -- 应收分保寿险责任准备金
    rr_reins_lthins_liab        NUMERIC(24,4),             -- 应收分保长期健康险责任准备金
    refund_depos                NUMERIC(24,4),             -- 存出保证金
    ph_pledge_loans             NUMERIC(24,4),             -- 保户质押贷款
    refund_cap_depos            NUMERIC(24,4),             -- 存出资本保证金
    indep_acct_assets           NUMERIC(24,4),             -- 独立账户资产
    client_depos                NUMERIC(24,4),             -- 其中：客户资金存款
    client_prov                 NUMERIC(24,4),             -- 其中：客户备付金
    transac_seat_fee            NUMERIC(24,4),             -- 其中:交易席位费
    invest_as_receiv            NUMERIC(24,4),             -- 应收款项类投资
    total_assets                NUMERIC(24,4),             -- 资产总计
    lt_borr                     NUMERIC(24,4),             -- 长期借款
    st_borr                     NUMERIC(24,4),             -- 短期借款
    cb_borr                     NUMERIC(24,4),             -- 向中央银行借款
    depos_ib_deposits           NUMERIC(24,4),             -- 吸收存款及同业存放
    loan_oth_bank               NUMERIC(24,4),             -- 拆入资金
    trading_fl                  NUMERIC(24,4),             -- 交易性金融负债
    notes_payable               NUMERIC(24,4),             -- 应付票据
    acct_payable                NUMERIC(24,4),             -- 应付账款
    adv_receipts                NUMERIC(24,4),             -- 预收款项
    sold_for_repur_fa           NUMERIC(24,4),             -- 卖出回购金融资产款
    comm_payable                NUMERIC(24,4),             -- 应付手续费及佣金
    payroll_payable             NUMERIC(24,4),             -- 应付职工薪酬
    taxes_payable               NUMERIC(24,4),             -- 应交税费
    int_payable                 NUMERIC(24,4),             -- 应付利息
    div_payable                 NUMERIC(24,4),             -- 应付股利
    oth_payable                 NUMERIC(24,4),             -- 其他应付款
    acc_exp                     NUMERIC(24,4),             -- 预提费用
    deferred_inc                NUMERIC(24,4),             -- 递延收益
    st_bonds_payable            NUMERIC(24,4),             -- 应付短期债券
    payable_to_reinsurer        NUMERIC(24,4),             -- 应付分保账款
    rsrv_insur_cont             NUMERIC(24,4),             -- 保险合同准备金
    acting_trading_sec          NUMERIC(24,4),             -- 代理买卖证券款
    acting_uw_sec               NUMERIC(24,4),             -- 代理承销证券款
    non_cur_liab_due_1y         NUMERIC(24,4),             -- 一年内到期的非流动负债
    oth_cur_liab                NUMERIC(24,4),             -- 其他流动负债
    total_cur_liab              NUMERIC(24,4),             -- 流动负债合计
    bond_payable                NUMERIC(24,4),             -- 应付债券
    lt_payable                  NUMERIC(24,4),             -- 长期应付款
    specific_payables           NUMERIC(24,4),             -- 专项应付款
    estimated_liab              NUMERIC(24,4),             -- 预计负债
    defer_tax_liab              NUMERIC(24,4),             -- 递延所得税负债
    defer_inc_non_cur_liab      NUMERIC(24,4),             -- 递延收益-非流动负债
    oth_ncl                     NUMERIC(24,4),             -- 其他非流动负债
    total_ncl                   NUMERIC(24,4),             -- 非流动负债合计
    depos_oth_bfi               NUMERIC(24,4),             -- 同业和其它金融机构存放款项
    deriv_liab                  NUMERIC(24,4),             -- 衍生金融负债
    depos                       NUMERIC(24,4),             -- 吸收存款
    agency_bus_liab             NUMERIC(24,4),             -- 代理业务负债
    oth_liab                    NUMERIC(24,4),             -- 其他负债
    prem_receiv_adva            NUMERIC(24,4),             -- 预收保费
    depos_received              NUMERIC(24,4),             -- 存入保证金
    ph_invest                   NUMERIC(24,4),             -- 保户储金及投资款
    reser_une_prem              NUMERIC(24,4),             -- 未到期责任准备金
    reser_outstd_claims         NUMERIC(24,4),             -- 未决赔款准备金
    reser_lins_liab             NUMERIC(24,4),             -- 寿险责任准备金
    reser_lthins_liab           NUMERIC(24,4),             -- 长期健康险责任准备金
    indept_acc_liab             NUMERIC(24,4),             -- 独立账户负债
    pledge_borr                 NUMERIC(24,4),             -- 其中:质押借款
    indem_payable               NUMERIC(24,4),             -- 应付赔付款
    policy_div_payable          NUMERIC(24,4),             -- 应付保单红利
    total_liab                  NUMERIC(24,4),             -- 负债合计
    treasury_share              NUMERIC(24,4),             -- 减:库存股
    ordin_risk_reser            NUMERIC(24,4),             -- 一般风险准备
    forex_differ                NUMERIC(24,4),             -- 外币报表折算差额
    invest_loss_unconf          NUMERIC(24,4),             -- 未确认的投资损失
    minority_int                NUMERIC(24,4),             -- 少数股东权益
    total_hldr_eqy_exc_min_int  NUMERIC(24,4),             -- 股东权益合计(不含少数股东权益)
    total_hldr_eqy_inc_min_int  NUMERIC(24,4),             -- 股东权益合计(含少数股东权益)
    total_liab_hldr_eqy         NUMERIC(24,4),             -- 负债及股东权益总计
    lt_payroll_payable          NUMERIC(24,4),             -- 长期应付职工薪酬
    oth_comp_income             NUMERIC(24,4),             -- 其他综合收益
    oth_eqt_tools               NUMERIC(24,4),             -- 其他权益工具
    oth_eqt_tools_p_shr         NUMERIC(24,4),             -- 其他权益工具(优先股)
    lending_funds               NUMERIC(24,4),             -- 融出资金
    acc_receivable              NUMERIC(24,4),             -- 应收款项
    st_fin_payable              NUMERIC(24,4),             -- 应付短期融资款
    payables                    NUMERIC(24,4),             -- 应付款项
    hfs_assets                  NUMERIC(24,4),             -- 持有待售的资产
    hfs_sales                   NUMERIC(24,4),             -- 持有待售的负债
    cost_fin_assets             NUMERIC(24,4),             -- 以摊余成本计量的金融资产
    fair_value_fin_assets       NUMERIC(24,4),             -- 以公允价值计量且其变动计入其他综合收益的金融资产
    cip_total                   NUMERIC(24,4),             -- 在建工程(合计)(元)
    oth_pay_total               NUMERIC(24,4),             -- 其他应付款(合计)(元)
    long_pay_total              NUMERIC(24,4),             -- 长期应付款(合计)(元)
    debt_invest                 NUMERIC(24,4),             -- 债权投资(元)
    oth_debt_invest             NUMERIC(24,4),             -- 其他债权投资(元)
    oth_eq_invest               NUMERIC(24,4),             -- 其他权益工具投资(元)
    oth_illiq_fin_assets        NUMERIC(24,4),             -- 其他非流动金融资产(元)
    oth_eq_ppbond               NUMERIC(24,4),             -- 其他权益工具:永续债(元)
    receiv_financing            NUMERIC(24,4),             -- 应收款项融资
    use_right_assets            NUMERIC(24,4),             -- 使用权资产
    lease_liab                  NUMERIC(24,4),             -- 租赁负债
    contract_assets             NUMERIC(24,4),             -- 合同资产
    contract_liab               NUMERIC(24,4),             -- 合同负债
    accounts_receiv_bill        NUMERIC(24,4),             -- 应收票据及应收账款
    accounts_pay                NUMERIC(24,4),             -- 应付票据及应付账款
    oth_rcv_total               NUMERIC(24,4),             -- 其他应收款(合计)（元）
    fix_assets_total            NUMERIC(24,4),             -- 固定资产(合计)(元)
    update_flag                 VARCHAR(4),
    created_at                  TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.balancesheet IS '资产负债表(Tushare官方158字段1:1)';
"""

# ---------- 现金流量表 (Tushare 官方 97 字段 1:1, 2026-10 官网输出参数表) ----------
DDL_CASHFLOW = """
CREATE TABLE IF NOT EXISTS fin.cashflow (
    ts_code          VARCHAR(16) NOT NULL,     -- TS股票代码
    ann_date         DATE,                     -- 公告日期
    f_ann_date       DATE,                     -- 实际公告日期
    end_date         DATE NOT NULL,            -- 报告期
    report_type      VARCHAR(2) NOT NULL DEFAULT '1', -- 报表类型
    comp_type        VARCHAR(2),               -- 公司类型(1一般工商业2银行3保险4证券7多元金融)
    end_type         VARCHAR(2),               -- 报告期类型
    net_profit       NUMERIC(24,4),            -- 净利润
    finan_exp        NUMERIC(24,4),            -- 财务费用
    c_fr_sale_sg     NUMERIC(24,4),            -- 销售商品、提供劳务收到的现金
    recp_tax_rends   NUMERIC(24,4),            -- 收到的税费返还
    n_depos_incr_fi  NUMERIC(24,4),            -- 客户存款和同业存放款项净增加额
    n_incr_loans_cb  NUMERIC(24,4),            -- 向中央银行借款净增加额
    n_inc_borr_oth_fi NUMERIC(24,4),           -- 向其他金融机构拆入资金净增加额
    prem_fr_orig_contr NUMERIC(24,4),          -- 收到原保险合同保费取得的现金
    n_incr_insured_dep NUMERIC(24,4),          -- 保户储金净增加额
    n_reinsur_prem   NUMERIC(24,4),            -- 收到再保业务现金净额
    n_incr_disp_tfa  NUMERIC(24,4),            -- 处置交易性金融资产净增加额
    ifc_cash_incr    NUMERIC(24,4),            -- 收取利息和手续费净增加额
    n_incr_disp_faas NUMERIC(24,4),            -- 处置可供出售金融资产净增加额
    n_incr_loans_oth_bank NUMERIC(24,4),       -- 拆入资金净增加额
    n_cap_incr_repur NUMERIC(24,4),            -- 回购业务资金净增加额
    c_fr_oth_operate_a NUMERIC(24,4),          -- 收到其他与经营活动有关的现金
    c_inf_fr_operate_a NUMERIC(24,4),          -- 经营活动现金流入小计
    c_paid_goods_s   NUMERIC(24,4),            -- 购买商品、接受劳务支付的现金
    c_paid_to_for_empl NUMERIC(24,4),          -- 支付给职工以及为职工支付的现金
    c_paid_for_taxes NUMERIC(24,4),            -- 支付的各项税费
    n_incr_clt_loan_adv NUMERIC(24,4),         -- 客户贷款及垫款净增加额
    n_incr_dep_cbob  NUMERIC(24,4),            -- 存放央行和同业款项净增加额
    c_pay_claims_orig_inco NUMERIC(24,4),      -- 支付原保险合同赔付款项的现金
    pay_handling_chrg NUMERIC(24,4),           -- 支付手续费的现金
    pay_comm_insur_plcy NUMERIC(24,4),         -- 支付保单红利的现金
    oth_cash_pay_oper_act NUMERIC(24,4),       -- 支付其他与经营活动有关的现金
    st_cash_out_act  NUMERIC(24,4),            -- 经营活动现金流出小计
    n_cashflow_act   NUMERIC(24,4),            -- 经营活动产生的现金流量净额
    oth_recp_ral_inv_act NUMERIC(24,4),        -- 收到其他与投资活动有关的现金
    c_disp_withdrwl_invest NUMERIC(24,4),      -- 收回投资收到的现金
    c_recp_return_invest NUMERIC(24,4),        -- 取得投资收益收到的现金
    n_recp_disp_fiolta NUMERIC(24,4),          -- 处置固定资产、无形资产和其他长期资产收回的现金净额
    n_recp_disp_sobu NUMERIC(24,4),            -- 处置子公司及其他营业单位收到的现金净额
    stot_inflows_inv_act NUMERIC(24,4),        -- 投资活动现金流入小计
    c_pay_acq_const_fiolta NUMERIC(24,4),      -- 购建固定资产、无形资产和其他长期资产支付的现金
    c_paid_invest    NUMERIC(24,4),            -- 投资支付的现金
    n_disp_subs_oth_biz NUMERIC(24,4),         -- 取得子公司及其他营业单位支付的现金净额
    oth_pay_ral_inv_act NUMERIC(24,4),         -- 支付其他与投资活动有关的现金
    n_incr_pledge_loan NUMERIC(24,4),          -- 质押贷款净增加额
    stot_out_inv_act NUMERIC(24,4),            -- 投资活动现金流出小计
    n_cashflow_inv_act NUMERIC(24,4),          -- 投资活动产生的现金流量净额
    c_recp_borrow    NUMERIC(24,4),            -- 取得借款收到的现金
    proc_issue_bonds NUMERIC(24,4),            -- 发行债券收到的现金
    oth_cash_recp_ral_fnc_act NUMERIC(24,4),   -- 收到其他与筹资活动有关的现金
    stot_cash_in_fnc_act NUMERIC(24,4),        -- 筹资活动现金流入小计
    free_cashflow    NUMERIC(24,4),            -- 企业自由现金流量
    c_prepay_amt_borr NUMERIC(24,4),           -- 偿还债务支付的现金
    c_pay_dist_dpcp_int_exp NUMERIC(24,4),     -- 分配股利、利润或偿付利息支付的现金
    incl_dvd_profit_paid_sc_ms NUMERIC(24,4),  -- 其中:子公司支付给少数股东的股利、利润
    oth_cashpay_ral_fnc_act NUMERIC(24,4),     -- 支付其他与筹资活动有关的现金
    stot_cashout_fnc_act NUMERIC(24,4),        -- 筹资活动现金流出小计
    n_cash_flows_fnc_act NUMERIC(24,4),        -- 筹资活动产生的现金流量净额
    eff_fx_flu_cash  NUMERIC(24,4),            -- 汇率变动对现金的影响
    n_incr_cash_cash_equ NUMERIC(24,4),        -- 现金及现金等价物净增加额
    c_cash_equ_beg_period NUMERIC(24,4),       -- 期初现金及现金等价物余额
    c_cash_equ_end_period NUMERIC(24,4),       -- 期末现金及现金等价物余额
    c_recp_cap_contrib NUMERIC(24,4),          -- 吸收投资收到的现金
    incl_cash_rec_saims NUMERIC(24,4),         -- 其中:子公司吸收少数股东投资收到的现金
    uncon_invest_loss NUMERIC(24,4),           -- 未确认投资损失
    prov_depr_assets NUMERIC(24,4),            -- 加:资产减值准备
    depr_fa_coga_dpba NUMERIC(24,4),           -- 固定资产折旧、油气资产折耗、生产性生物资产折旧
    amort_intang_assets NUMERIC(24,4),         -- 无形资产摊销
    lt_amort_deferred_exp NUMERIC(24,4),       -- 长期待摊费用摊销
    decr_deferred_exp NUMERIC(24,4),           -- 待摊费用减少
    incr_acc_exp     NUMERIC(24,4),            -- 预提费用增加
    loss_disp_fiolta NUMERIC(24,4),            -- 处置固定、无形资产和其他长期资产的损失
    loss_scr_fa      NUMERIC(24,4),            -- 固定资产报废损失
    loss_fv_chg      NUMERIC(24,4),            -- 公允价值变动损失
    invest_loss      NUMERIC(24,4),            -- 投资损失
    decr_def_inc_tax_assets NUMERIC(24,4),     -- 递延所得税资产减少
    incr_def_inc_tax_liab NUMERIC(24,4),       -- 递延所得税负债增加
    decr_inventories NUMERIC(24,4),            -- 存货的减少
    decr_oper_payable NUMERIC(24,4),           -- 经营性应收项目的减少
    incr_oper_payable NUMERIC(24,4),           -- 经营性应付项目的增加
    others           NUMERIC(24,4),            -- 其他
    im_net_cashflow_oper_act NUMERIC(24,4),    -- 经营活动产生的现金流量净额(间接法)
    conv_debt_into_cap NUMERIC(24,4),          -- 债务转为资本
    conv_copbonds_due_within_1y NUMERIC(24,4), -- 一年内到期的可转换公司债券
    fa_fnc_leases    NUMERIC(24,4),            -- 融资租入固定资产
    im_n_incr_cash_equ NUMERIC(24,4),          -- 现金及现金等价物净增加额(间接法)
    net_dism_capital_add NUMERIC(24,4),        -- 拆出资金净增加额
    net_cash_rece_sec NUMERIC(24,4),           -- 代理买卖证券收到的现金净额(元)
    credit_impa_loss NUMERIC(24,4),            -- 信用减值损失
    use_right_asset_dep NUMERIC(24,4),         -- 使用权资产折旧
    oth_loss_asset   NUMERIC(24,4),            -- 其他资产减值损失
    end_bal_cash     NUMERIC(24,4),            -- 现金的期末余额
    beg_bal_cash     NUMERIC(24,4),            -- 减:现金的期初余额
    end_bal_cash_equ NUMERIC(24,4),            -- 加:现金等价物的期末余额
    beg_bal_cash_equ NUMERIC(24,4),            -- 减:现金等价物的期初余额
    update_flag      VARCHAR(4),               -- 更新标志(1最新）
    created_at       TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.cashflow IS '现金流量表(Tushare官方97字段1:1)';
"""

# ---------- 财务指标 (Tushare 官方 167 字段 1:1 + 本地 report_type 主键列, 2026-10 官网) ----------
DDL_FINA_INDICATOR = """
CREATE TABLE IF NOT EXISTS fin.fina_indicator (
    ts_code                       VARCHAR(16) NOT NULL,       -- TS代码
    ann_date                      DATE,                       -- 公告日期
    end_date                      DATE NOT NULL,              -- 报告期
    report_type                   VARCHAR(2) NOT NULL DEFAULT '1', -- 本地主键列(官方无此输出, 勿删)
    eps                           NUMERIC(20,6),              -- 基本每股收益
    dt_eps                        NUMERIC(20,6),              -- 稀释每股收益
    total_revenue_ps              NUMERIC(20,6),              -- 每股营业总收入
    revenue_ps                    NUMERIC(20,6),              -- 每股营业收入
    capital_rese_ps               NUMERIC(20,6),              -- 每股资本公积
    surplus_rese_ps               NUMERIC(20,6),              -- 每股盈余公积
    undist_profit_ps              NUMERIC(20,6),              -- 每股未分配利润
    extra_item                    NUMERIC(20,6),              -- 非经常性损益
    profit_dedt                   NUMERIC(20,6),              -- 扣除非经常性损益后的净利润（扣非净利润）
    gross_margin                  NUMERIC(20,6),              -- 毛利
    current_ratio                 NUMERIC(20,6),              -- 流动比率
    quick_ratio                   NUMERIC(20,6),              -- 速动比率
    cash_ratio                    NUMERIC(20,6),              -- 保守速动比率
    invturn_days                  NUMERIC(20,6),              -- 存货周转天数
    arturn_days                   NUMERIC(20,6),              -- 应收账款周转天数
    inv_turn                      NUMERIC(20,6),              -- 存货周转率
    ar_turn                       NUMERIC(20,6),              -- 应收账款周转率
    ca_turn                       NUMERIC(20,6),              -- 流动资产周转率
    fa_turn                       NUMERIC(20,6),              -- 固定资产周转率
    assets_turn                   NUMERIC(20,6),              -- 总资产周转率
    op_income                     NUMERIC(20,6),              -- 经营活动净收益
    valuechange_income            NUMERIC(20,6),              -- 价值变动净收益
    interst_income                NUMERIC(20,6),              -- 利息费用
    daa                           NUMERIC(20,6),              -- 折旧与摊销
    ebit                          NUMERIC(20,6),              -- 息税前利润
    ebitda                        NUMERIC(20,6),              -- 息税折旧摊销前利润
    fcff                          NUMERIC(20,6),              -- 企业自由现金流量
    fcfe                          NUMERIC(20,6),              -- 股权自由现金流量
    current_exint                 NUMERIC(20,6),              -- 无息流动负债
    noncurrent_exint              NUMERIC(20,6),              -- 无息非流动负债
    interestdebt                  NUMERIC(20,6),              -- 带息债务
    netdebt                       NUMERIC(20,6),              -- 净债务
    tangible_asset                NUMERIC(20,6),              -- 有形资产
    working_capital               NUMERIC(20,6),              -- 营运资金
    networking_capital            NUMERIC(20,6),              -- 营运流动资本
    invest_capital                NUMERIC(20,6),              -- 全部投入资本
    retained_earnings             NUMERIC(20,6),              -- 留存收益
    diluted2_eps                  NUMERIC(20,6),              -- 期末摊薄每股收益
    bps                           NUMERIC(20,6),              -- 每股净资产
    ocfps                         NUMERIC(20,6),              -- 每股经营活动产生的现金流量净额
    retainedps                    NUMERIC(20,6),              -- 每股留存收益
    cfps                          NUMERIC(20,6),              -- 每股现金流量净额
    ebit_ps                       NUMERIC(20,6),              -- 每股息税前利润
    fcff_ps                       NUMERIC(20,6),              -- 每股企业自由现金流量
    fcfe_ps                       NUMERIC(20,6),              -- 每股股东自由现金流量
    netprofit_margin              NUMERIC(20,6),              -- 销售净利率
    grossprofit_margin            NUMERIC(20,6),              -- 销售毛利率
    cogs_of_sales                 NUMERIC(20,6),              -- 销售成本率
    expense_of_sales              NUMERIC(20,6),              -- 销售期间费用率
    profit_to_gr                  NUMERIC(20,6),              -- 净利润/营业总收入
    saleexp_to_gr                 NUMERIC(20,6),              -- 销售费用/营业总收入
    adminexp_of_gr                NUMERIC(20,6),              -- 管理费用/营业总收入
    finaexp_of_gr                 NUMERIC(20,6),              -- 财务费用/营业总收入
    impai_ttm                     NUMERIC(20,6),              -- 资产减值损失/营业总收入
    gc_of_gr                      NUMERIC(20,6),              -- 营业总成本/营业总收入
    op_of_gr                      NUMERIC(20,6),              -- 营业利润/营业总收入
    ebit_of_gr                    NUMERIC(20,6),              -- 息税前利润/营业总收入
    roe                           NUMERIC(20,6),              -- 净资产收益率
    roe_waa                       NUMERIC(20,6),              -- 加权平均净资产收益率
    roe_dt                        NUMERIC(20,6),              -- 净资产收益率(扣除非经常损益)
    roa                           NUMERIC(20,6),              -- 总资产报酬率
    npta                          NUMERIC(20,6),              -- 总资产净利润
    roic                          NUMERIC(20,6),              -- 投入资本回报率
    roe_yearly                    NUMERIC(20,6),              -- 年化净资产收益率
    roa2_yearly                   NUMERIC(20,6),              -- 年化总资产报酬率
    roe_avg                       NUMERIC(20,6),              -- 平均净资产收益率(增发条件)
    opincome_of_ebt               NUMERIC(20,6),              -- 经营活动净收益/利润总额
    investincome_of_ebt           NUMERIC(20,6),              -- 价值变动净收益/利润总额
    n_op_profit_of_ebt            NUMERIC(20,6),              -- 营业外收支净额/利润总额
    tax_to_ebt                    NUMERIC(20,6),              -- 所得税/利润总额
    dtprofit_to_profit            NUMERIC(20,6),              -- 扣除非经常损益后的净利润/净利润
    salescash_to_or               NUMERIC(20,6),              -- 销售商品提供劳务收到的现金/营业收入
    ocf_to_or                     NUMERIC(20,6),              -- 经营活动产生的现金流量净额/营业收入
    ocf_to_opincome               NUMERIC(20,6),              -- 经营活动产生的现金流量净额/经营活动净收益
    capitalized_to_da             NUMERIC(20,6),              -- 资本支出/折旧和摊销
    debt_to_assets                NUMERIC(20,6),              -- 资产负债率
    assets_to_eqt                 NUMERIC(20,6),              -- 权益乘数
    dp_assets_to_eqt              NUMERIC(20,6),              -- 权益乘数(杜邦分析)
    ca_to_assets                  NUMERIC(20,6),              -- 流动资产/总资产
    nca_to_assets                 NUMERIC(20,6),              -- 非流动资产/总资产
    tbassets_to_totalassets       NUMERIC(20,6),              -- 有形资产/总资产
    int_to_talcap                 NUMERIC(20,6),              -- 带息债务/全部投入资本
    eqt_to_talcapital             NUMERIC(20,6),              -- 归属于母公司的股东权益/全部投入资本
    currentdebt_to_debt           NUMERIC(20,6),              -- 流动负债/负债合计
    longdeb_to_debt               NUMERIC(20,6),              -- 非流动负债/负债合计
    ocf_to_shortdebt              NUMERIC(20,6),              -- 经营活动产生的现金流量净额/流动负债
    debt_to_eqt                   NUMERIC(20,6),              -- 产权比率
    eqt_to_debt                   NUMERIC(20,6),              -- 归属于母公司的股东权益/负债合计
    eqt_to_interestdebt           NUMERIC(20,6),              -- 归属于母公司的股东权益/带息债务
    tangibleasset_to_debt         NUMERIC(20,6),              -- 有形资产/负债合计
    tangasset_to_intdebt          NUMERIC(20,6),              -- 有形资产/带息债务
    tangibleasset_to_netdebt      NUMERIC(20,6),              -- 有形资产/净债务
    ocf_to_debt                   NUMERIC(20,6),              -- 经营活动产生的现金流量净额/负债合计
    ocf_to_interestdebt           NUMERIC(20,6),              -- 经营活动产生的现金流量净额/带息债务
    ocf_to_netdebt                NUMERIC(20,6),              -- 经营活动产生的现金流量净额/净债务
    ebit_to_interest              NUMERIC(20,6),              -- 已获利息倍数(EBIT/利息费用)
    longdebt_to_workingcapital    NUMERIC(20,6),              -- 长期债务与营运资金比率
    ebitda_to_debt                NUMERIC(20,6),              -- 息税折旧摊销前利润/负债合计
    turn_days                     NUMERIC(20,6),              -- 营业周期
    roa_yearly                    NUMERIC(20,6),              -- 年化总资产净利率
    roa_dp                        NUMERIC(20,6),              -- 总资产净利率(杜邦分析)
    fixed_assets                  NUMERIC(20,6),              -- 固定资产合计
    profit_prefin_exp             NUMERIC(20,6),              -- 扣除财务费用前营业利润
    non_op_profit                 NUMERIC(20,6),              -- 非营业利润
    op_to_ebt                     NUMERIC(20,6),              -- 营业利润／利润总额
    nop_to_ebt                    NUMERIC(20,6),              -- 非营业利润／利润总额
    ocf_to_profit                 NUMERIC(20,6),              -- 经营活动产生的现金流量净额／营业利润
    cash_to_liqdebt               NUMERIC(20,6),              -- 货币资金／流动负债
    cash_to_liqdebt_withinterest  NUMERIC(20,6),              -- 货币资金／带息流动负债
    op_to_liqdebt                 NUMERIC(20,6),              -- 营业利润／流动负债
    op_to_debt                    NUMERIC(20,6),              -- 营业利润／负债合计
    roic_yearly                   NUMERIC(20,6),              -- 年化投入资本回报率
    total_fa_trun                 NUMERIC(20,6),              -- 固定资产合计周转率
    profit_to_op                  NUMERIC(20,6),              -- 利润总额／营业收入
    q_opincome                    NUMERIC(20,6),              -- 经营活动单季度净收益
    q_investincome                NUMERIC(20,6),              -- 价值变动单季度净收益
    q_dtprofit                    NUMERIC(20,6),              -- 扣除非经常损益后的单季度净利润
    q_eps                         NUMERIC(20,6),              -- 每股收益(单季度)
    q_netprofit_margin            NUMERIC(20,6),              -- 销售净利率(单季度)
    q_gsprofit_margin             NUMERIC(20,6),              -- 销售毛利率(单季度)
    q_exp_to_sales                NUMERIC(20,6),              -- 销售期间费用率(单季度)
    q_profit_to_gr                NUMERIC(20,6),              -- 净利润／营业总收入(单季度)
    q_saleexp_to_gr               NUMERIC(20,6),              -- 销售费用／营业总收入 (单季度)
    q_adminexp_to_gr              NUMERIC(20,6),              -- 管理费用／营业总收入 (单季度)
    q_finaexp_to_gr               NUMERIC(20,6),              -- 财务费用／营业总收入 (单季度)
    q_impair_to_gr_ttm            NUMERIC(20,6),              -- 资产减值损失／营业总收入(单季度)
    q_gc_to_gr                    NUMERIC(20,6),              -- 营业总成本／营业总收入 (单季度)
    q_op_to_gr                    NUMERIC(20,6),              -- 营业利润／营业总收入(单季度)
    q_roe                         NUMERIC(20,6),              -- 净资产收益率(单季度)
    q_dt_roe                      NUMERIC(20,6),              -- 净资产单季度收益率(扣除非经常损益)
    q_npta                        NUMERIC(20,6),              -- 总资产净利润(单季度)
    q_opincome_to_ebt             NUMERIC(20,6),              -- 经营活动净收益／利润总额(单季度)
    q_investincome_to_ebt         NUMERIC(20,6),              -- 价值变动净收益／利润总额(单季度)
    q_dtprofit_to_profit          NUMERIC(20,6),              -- 扣除非经常损益后的净利润／净利润(单季度)
    q_salescash_to_or             NUMERIC(20,6),              -- 销售商品提供劳务收到的现金／营业收入(单季度)
    q_ocf_to_sales                NUMERIC(20,6),              -- 经营活动产生的现金流量净额／营业收入(单季度)
    q_ocf_to_or                   NUMERIC(20,6),              -- 经营活动产生的现金流量净额／经营活动净收益(单季度)
    basic_eps_yoy                 NUMERIC(20,6),              -- 基本每股收益同比增长率(%)
    dt_eps_yoy                    NUMERIC(20,6),              -- 稀释每股收益同比增长率(%)
    cfps_yoy                      NUMERIC(20,6),              -- 每股经营活动产生的现金流量净额同比增长率(%)
    op_yoy                        NUMERIC(20,6),              -- 营业利润同比增长率(%)
    ebt_yoy                       NUMERIC(20,6),              -- 利润总额同比增长率(%)
    netprofit_yoy                 NUMERIC(20,6),              -- 归属母公司股东的净利润同比增长率(%)
    dt_netprofit_yoy              NUMERIC(20,6),              -- 归属母公司股东的净利润-扣除非经常损益同比增长率(%)
    ocf_yoy                       NUMERIC(20,6),              -- 经营活动产生的现金流量净额同比增长率(%)
    roe_yoy                       NUMERIC(20,6),              -- 净资产收益率(摊薄)同比增长率(%)
    bps_yoy                       NUMERIC(20,6),              -- 每股净资产相对年初增长率(%)
    assets_yoy                    NUMERIC(20,6),              -- 资产总计相对年初增长率(%)
    eqt_yoy                       NUMERIC(20,6),              -- 归属母公司的股东权益相对年初增长率(%)
    tr_yoy                        NUMERIC(20,6),              -- 营业总收入同比增长率(%)
    or_yoy                        NUMERIC(20,6),              -- 营业收入同比增长率(%)
    q_gr_yoy                      NUMERIC(20,6),              -- 营业总收入同比增长率(%)(单季度)
    q_gr_qoq                      NUMERIC(20,6),              -- 营业总收入环比增长率(%)(单季度)
    q_sales_yoy                   NUMERIC(20,6),              -- 营业收入同比增长率(%)(单季度)
    q_sales_qoq                   NUMERIC(20,6),              -- 营业收入环比增长率(%)(单季度)
    q_op_yoy                      NUMERIC(20,6),              -- 营业利润同比增长率(%)(单季度)
    q_op_qoq                      NUMERIC(20,6),              -- 营业利润环比增长率(%)(单季度)
    q_profit_yoy                  NUMERIC(20,6),              -- 净利润同比增长率(%)(单季度)
    q_profit_qoq                  NUMERIC(20,6),              -- 净利润环比增长率(%)(单季度)
    q_netprofit_yoy               NUMERIC(20,6),              -- 归属母公司股东的净利润同比增长率(%)(单季度)
    q_netprofit_qoq               NUMERIC(20,6),              -- 归属母公司股东的净利润环比增长率(%)(单季度)
    equity_yoy                    NUMERIC(20,6),              -- 净资产同比增长率
    rd_exp                        NUMERIC(20,6),              -- 研发投入合计
    update_flag                   VARCHAR(4),                 -- 更新标识
    created_at                    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, report_type)
);
COMMENT ON TABLE fin.fina_indicator IS '财务指标(Tushare官方167字段1:1, 另含本地report_type主键列)';
"""

# ---------- 分红送股 (Tushare 官方 16 字段 1:1, 2026-10 官网输出参数表) ----------
# 官方无唯一键: PK 取 (ts_code, end_date, ann_date, div_proc) —— 四列官方"默认显示 Y";
# 多阶段行(预案/决案/实施)由 div_proc 区分, 同年多次分红由 ann_date 区分;
# 实施类日期列(ex_date 等)在预案阶段必 NULL, 不可入 PK。
# 实测注: 个别早期阶段行(如"股东提议") ann_date 实为 NULL, 同步层跳过该类行(PK 列不可空)
DDL_DIVIDEND = """
CREATE TABLE IF NOT EXISTS fin.dividend (
    ts_code       VARCHAR(16) NOT NULL,   -- TS代码
    end_date      DATE NOT NULL,          -- 分红年度
    ann_date      DATE NOT NULL,          -- 公告日(预案，决案)
    div_proc      VARCHAR(16) NOT NULL,   -- 实施进度
    stk_div       NUMERIC(12,6),          -- 每股送转
    stk_bo_rate   NUMERIC(12,6),          -- 每股送股比例
    stk_co_rate   NUMERIC(12,6),          -- 每股转增比例
    cash_div      NUMERIC(12,6),          -- 每股分红（税后）
    cash_div_tax  NUMERIC(12,6),          -- 每股分红（税前）
    record_date   DATE,                   -- 股权登记日
    ex_date       DATE,                   -- 除权除息日
    pay_date      DATE,                   -- 派息日
    div_listdate  DATE,                   -- 红股上市日
    imp_ann_date  DATE,                   -- 实施公告日
    base_date     DATE,                   -- 基准日
    base_share    NUMERIC(20,4),          -- 基准股本（万）
    created_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, ann_date, div_proc)
);
COMMENT ON TABLE fin.dividend IS '分红送股(Tushare官方16字段1:1)';
"""

# ---------- 财务审计意见 (Tushare 官方 7 字段 1:1, 2026-10 官网输出参数表) ----------
# 官方无唯一键: PK 取 (ts_code, end_date, ann_date) —— 三列官方恒有值;
# 年报更正/重述会为同一 end_date 再次公告审计意见, ann_date 区分保留意见变更历史;
# 查最新意见按 (ts_code, end_date) 取 max(ann_date)
DDL_FINA_AUDIT = """
CREATE TABLE IF NOT EXISTS fin.fina_audit (
    ts_code       VARCHAR(16) NOT NULL,   -- TS股票代码
    ann_date      DATE NOT NULL,          -- 公告日期
    end_date      DATE NOT NULL,          -- 报告期
    audit_result  VARCHAR(64),            -- 审计结果
    audit_fees    NUMERIC(20,4),          -- 审计总费用（元）
    audit_agency  VARCHAR(128),           -- 会计事务所
    audit_sign    VARCHAR(128),           -- 签字会计师
    created_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, ann_date)
);
COMMENT ON TABLE fin.fina_audit IS '财务审计意见(Tushare官方7字段1:1)';
"""

# ---------- 财报披露日期 (Tushare 官方 6 字段 1:1, 2026-10 官网输出参数表) ----------
# 官方无唯一键: PK 取 (ts_code, end_date) —— 一家股票一个报告期一条披露计划(最新状态行);
# ann_date"最新披露公告日"/modify_date"披露日期修正记录"均为同行覆盖语义(修正不产生新行);
# ann_date 不能保证恒有值(预约计划可先于公告存在), actual_date 披露前必 NULL, 均不可入 PK
DDL_DISCLOSURE_DATE = """
CREATE TABLE IF NOT EXISTS fin.disclosure_date (
    ts_code       VARCHAR(16) NOT NULL,   -- TS代码
    ann_date      DATE,                   -- 最新披露公告日
    end_date      DATE NOT NULL,          -- 报告期
    pre_date      DATE,                   -- 预计披露日期
    actual_date   DATE,                   -- 实际披露日期
    modify_date   DATE,                   -- 披露日期修正记录
    created_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date)
);
CREATE INDEX IF NOT EXISTS idx_disclosure_actual ON fin.disclosure_date (actual_date);
COMMENT ON TABLE fin.disclosure_date IS '财报披露日期(Tushare官方6字段1:1)';
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

# ---------- 主营构成 (fina_mainbz, Tushare 官方 9 字段 1:1, 2026-10 官网输出参数表) ----------
DDL_FINA_MAINBZ = """
CREATE TABLE IF NOT EXISTS fin.fina_mainbz (
    ts_code       VARCHAR(16) NOT NULL,
    end_date      DATE NOT NULL,               -- 报告期
    bz_item       VARCHAR(512) NOT NULL,       -- 主营构成项目 (产品/地区/行业名)
    bz_code       VARCHAR(16) NOT NULL,        -- 主营业务来源类型: P-按产品 D-按地区 I-按行业 (S-销售模式; 存量另含 455006000/F 等补充维度编码, 原样保留)
    bz_sales      NUMERIC(24,4),               -- 主营业务收入 (元)
    bz_profit     NUMERIC(24,4),               -- 主营业务利润 (元)
    bz_cost       NUMERIC(24,4),               -- 主营业务成本 (元)
    curr_type     VARCHAR(8),                  -- 货币代码 (CNY)
    update_flag   VARCHAR(4),                  -- 是否更新 (官方输出字段; 当前接口不返回, 恒为NULL)
    created_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, bz_code, bz_item)
);
CREATE INDEX IF NOT EXISTS idx_fina_mainbz_end_date ON fin.fina_mainbz (end_date);
COMMENT ON TABLE fin.fina_mainbz IS '主营业务构成原始表 (Tushare官方9字段1:1; 公司x报告期x维度x项目, 金额单位:元; 一次调用返回全部维度, bz_code 即维度标签)';
"""

# 主营构成清洗视图: 剔除维度表头行(产品/行业/地区)与合计, "其中"/冒号分层子项不计入占比分母, 派生收入占比与毛利率
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

# ---------- 巨潮公告 (cninfo) ----------
# 2026-10 起 tools/cninfo 解耦为自包含模块 (零项目依赖), 元数据/进度/orgId 映射
# 改为文件产物 output/cninfo/meta/ (tools/cninfo/store.py), 不再写库。
# 存量表 fin.cninfo_announcement / fin.cninfo_sync_state / fin.cninfo_org_map
# 保留历史数据未迁移, 仅作历史查询用途。





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

# 财务表名常量
T_INCOME = "fin.income"
T_BALANCESHEET = "fin.balancesheet"
T_CASHFLOW = "fin.cashflow"
T_FINA_INDICATOR = "fin.fina_indicator"
T_DIVIDEND = "fin.dividend"
T_FINA_AUDIT = "fin.fina_audit"
T_DISCLOSURE_DATE = "fin.disclosure_date"
T_FINA_MAINBZ = "fin.fina_mainbz"
T_FIN_SYNC_META = "fin.sync_meta"

# 产业链图谱表 fin.chain_extract 的 DDL 已外迁至 beta_alpha/schema.py
# (模块内 ensure 函数, 随 beta_alpha 子系统自治)


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
    ts_code         VARCHAR(16),               -- 标的代码 (可空: 非A股研报无 ts_code; 旧库 NOT NULL 由 ensure 放宽)
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

# 去重合并删除留痕: sync 幂等靠 file_id 存在性检查, 物理删除后 file_id 消失
# 会被 _sync_group_files (全量扫描) 重新插入 -> 入库/删除/重插/再分析 乒乓循环 (曾致
# 同一图片型 PDF 反复 OCR 烧 qwen)。删除前落此表, sync 检查它后永不重插。
DDL_REPORT_MERGED = """
CREATE TABLE IF NOT EXISTS fin.report_meta_merged (
    file_id     BIGINT PRIMARY KEY,            -- 被合并删除的文件ID
    report_id   INTEGER,
    topic_id    BIGINT,
    file_name   VARCHAR(256),
    merged_at   TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE fin.report_meta_merged IS '去重合并删除留痕: 已删条目不再被 sync 重新插入';
"""

REPORT_DDL = [
    DDL_REPORT_META,
    DDL_REPORT_FORECAST,
    DDL_REPORT_SYNC_META,
    DDL_REPORT_MERGED,
]

T_REPORT_META = "fin.report_meta"
T_REPORT_FORECAST = "fin.report_forecast"
T_REPORT_SYNC_META = "fin.report_sync_meta"


def init_report_schema(pg_client) -> None:
    """部署研报相关表 (幂等)"""
    for ddl in REPORT_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()


# (v_report_ready 视图已删, 2026-10-11: 全库零查询消费、未进 LLM 数据字典, 纯遗留;
#  需要时等价查询: SELECT ... FROM fin.report_meta WHERE extraction_status='extracted'
#  AND ts_code IS NOT NULL AND content_chars > 0)


# ============================================================
# 研报表增量列 — 单一事实源
# ============================================================
# report_meta / report_forecast 的扩展列与索引曾散落于
# tools/finance/report_meta_analysis.py (文件名推断列) 与
# skills/report/skill.py (LLM 深度提取列) 两处 ensure, 重构收敛于此;
# 两模块的 ensure_* 仅做委托调用。

DDL_REPORT_ANALYSIS_STATEMENTS = [
    # (语句, 表, 列名或索引名): 缺列/缺索引才执行 (幂等 + 避免无谓 ALTER 锁)
    # 文件名推断列
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS target VARCHAR(64)",
     "report_meta", "target"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS industry VARCHAR(64)",
     "report_meta", "industry"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS region VARCHAR(64)",
     "report_meta", "region"),
    ("CREATE INDEX IF NOT EXISTS idx_report_meta_industry ON fin.report_meta (industry)",
     "report_meta", "idx_report_meta_industry"),
    ("CREATE INDEX IF NOT EXISTS idx_report_meta_region ON fin.report_meta (region)",
     "report_meta", "idx_report_meta_region"),
    # LLM 深度提取列 (analysis_status 取值: none/done/failed/no_llm)
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS analysis_json TEXT",
     "report_meta", "analysis_json"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS analysis_status VARCHAR(16) DEFAULT 'none'",
     "report_meta", "analysis_status"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS market VARCHAR(8)",
     "report_meta", "market"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS symbols TEXT",
     "report_meta", "symbols"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_industries TEXT",
     "report_meta", "tags_industries"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_regions TEXT",
     "report_meta", "tags_regions"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_commodities TEXT",
     "report_meta", "tags_commodities"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_themes TEXT",
     "report_meta", "tags_themes"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_targets TEXT",
     "report_meta", "tags_targets"),
    # 正文中文译文 (英文原版入库时后台翻译写入; 中文正文留空, 读取侧回退原文)
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS content_text_cn TEXT",
     "report_meta", "content_text_cn"),
    ("ALTER TABLE fin.report_forecast ADD COLUMN IF NOT EXISTS symbol VARCHAR(32)",
     "report_forecast", "symbol"),
    ("ALTER TABLE fin.report_forecast ADD COLUMN IF NOT EXISTS market VARCHAR(8)",
     "report_forecast", "market"),
    ("CREATE INDEX IF NOT EXISTS idx_report_meta_market ON fin.report_meta (market)",
     "report_meta", "idx_report_meta_market"),
    ("CREATE INDEX IF NOT EXISTS idx_report_forecast_symbol ON fin.report_forecast (symbol)",
     "report_forecast", "idx_report_forecast_symbol"),
]

_report_analysis_cols_ensured = False


@contextmanager
def ddl_cursor():
    """DDL 专用独立连接 (autocommit, 用完即关)。

    ALTER/CREATE 持表级锁, 放业务事务里会一直锁到事务提交 (LLM 调用期间整表被堵),
    所有 ensure 类 DDL 一律走本连接立即提交。
    """
    cfg = pg_config()
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], dbname=cfg["database"], connect_timeout=10,
        options="-c search_path=stock,fin,public -c statement_timeout=180000",
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
    try:
        conn.autocommit = True
        yield conn.cursor()
    finally:
        conn.close()


def ensure_report_analysis_columns() -> None:
    """补齐研报表增量列/索引, 并放宽旧库 report_forecast.ts_code 可空 (幂等, 进程内一次)。

    ts_code 基表 DDL 已可空; 旧库曾以 NOT NULL 建表, 非 A 股研报无 ts_code, 此处兜底放宽。
    """
    global _report_analysis_cols_ensured
    if _report_analysis_cols_ensured:
        return
    with ddl_cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='fin' AND table_name IN ('report_meta','report_forecast')")
        existing = {(r[0], r[1]) for r in cur.fetchall()}
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='fin' "
            "AND tablename IN ('report_meta','report_forecast')")
        existing_idx = {r[0] for r in cur.fetchall()}
        need = []
        for stmt, table, key in DDL_REPORT_ANALYSIS_STATEMENTS:
            if stmt.startswith("CREATE INDEX"):
                if key not in existing_idx:
                    need.append(stmt)
            elif (table, key) not in existing:
                need.append(stmt)
        for stmt in need:
            cur.execute(stmt)
        if need:
            logger.info(f"研报表结构补列: {len(need)} 条 DDL")
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema='fin' AND table_name='report_forecast' "
            "AND column_name='ts_code'")
        row = cur.fetchone()
        if row and row[0] == "NO":
            cur.execute(
                "ALTER TABLE fin.report_forecast ALTER COLUMN ts_code DROP NOT NULL")
    _report_analysis_cols_ensured = True


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
