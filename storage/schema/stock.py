"""Stock schema definitions; SQL moved without changing table names."""
DDL_STOCK_SCHEMA = """CREATE SCHEMA IF NOT EXISTS stock"""

DDL_STOCK_BASIC = """
CREATE TABLE IF NOT EXISTS stock.stock_basic (
    ts_code      VARCHAR(16) PRIMARY KEY,   -- code, e.g. 000001.SZ
    symbol       VARCHAR(8)  NOT NULL,      -- 6-digit numeric code
    name         VARCHAR(32),               -- name
    area         VARCHAR(16),               -- region
    industry     VARCHAR(32),               -- industry
    market       VARCHAR(8),                -- market: main board / ChiNext / STAR
    list_date    DATE,                      -- listing date
    delist_date  DATE,                      -- delisting date (empty = still listed)
    is_hs        VARCHAR(4),                -- Shanghai/HK Stock Connect target or not
    status       VARCHAR(4),                -- L listed / D delisted / P suspended
    industry_l1  VARCHAR(32),               -- SW level-1 industry (current; denormalized for fast lookup)
    industry_l2  VARCHAR(32),               -- SW level-2 industry (current; denormalized for fast lookup)
    updated_at   TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE stock.stock_basic IS '股票基础信息(股票池)';
"""

DDL_TRADE_CALENDAR = """
CREATE TABLE IF NOT EXISTS stock.trade_calendar (
    exchange    VARCHAR(8)  NOT NULL,   -- exchange: SSE/SZSE
    cal_date    DATE        NOT NULL,   -- calendar date
    is_open     SMALLINT,               -- trading day 1/0
    pretrade_date DATE,                 -- previous trading day
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
    pre_close   NUMERIC(12,3),      -- previous close (unadjusted)
    change      NUMERIC(12,3),      -- price change
    pct_chg     NUMERIC(8,3),       -- change percent %
    vol         NUMERIC(18,2),      -- volume (lots)
    amount      NUMERIC(20,2),      -- turnover (thousand CNY)
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
    close       NUMERIC(12,3),       -- closing price
    turnover_rate   NUMERIC(12,4),   -- turnover rate %
    turnover_rate_f NUMERIC(12,4),   -- turnover rate (free float)
    volume_ratio    NUMERIC(12,4),   -- volume ratio
    pe          NUMERIC(16,4),       -- P/E (TTM)
    pe_ttm      NUMERIC(16,4),       -- P/E (TTM)
    pb          NUMERIC(16,4),       -- P/B
    ps          NUMERIC(16,4),       -- P/S TTM
    ps_ttm      NUMERIC(16,4),
    dv_ratio    NUMERIC(12,4),       -- dividend yield %
    dv_ttm      NUMERIC(12,4),
    total_share NUMERIC(20,4),       -- total shares (10k shares)
    float_share NUMERIC(20,4),       -- floating shares (10k shares)
    free_share  NUMERIC(20,4),       -- free-float shares (10k shares)
    total_mv    NUMERIC(20,4),       -- total market cap (10k CNY)
    circ_mv     NUMERIC(20,4),       -- circulating market cap (10k CNY)
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_daily_basic_ts_code ON stock.daily_basic (ts_code, trade_date);
COMMENT ON TABLE stock.daily_basic IS '每日估值与市场指标';
"""

DDL_SYNC_META = """
CREATE TABLE IF NOT EXISTS stock.sync_meta (
    table_name     VARCHAR(32) PRIMARY KEY,   -- daily / adj_factor / daily_basic
    latest_date    DATE,                      -- latest trading day synced
    total_rows     BIGINT DEFAULT 0,          -- cumulative rows synced
    updated_at     TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE stock.sync_meta IS '数据同步水位线(断点续传用)';
"""

# ---------- Top-10 holders (Tushare top10_holders; the single API already includes float ratio/changes) ----------
DDL_TOP10_HOLDERS = """
CREATE TABLE IF NOT EXISTS stock.top10_holders (
    ts_code          VARCHAR(16) NOT NULL,   -- TS code
    ann_date         DATE,                   -- announcement date
    end_date         DATE NOT NULL,          -- report period
    holder_name      VARCHAR(128) NOT NULL,  -- holder name
    hold_amount      NUMERIC(20,2),          -- shares held (10k shares)
    hold_ratio       NUMERIC(10,4),          -- holding ratio %
    hold_float_ratio NUMERIC(10,4),          -- ratio of floating shares held %
    hold_change      NUMERIC(20,2),          -- change in holding (10k shares)
    holder_type      VARCHAR(32),            -- holder nature (API returns Chinese: investment company / natural person / fund, etc.)
    updated_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (ts_code, end_date, holder_name, holder_type)
);
"""

# Table creation order (dependencies: stock_basic has none, daily_basic is standalone, all can run in parallel)
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

# Table name constants
T_STOCK_BASIC = "stock.stock_basic"
T_TRADE_CALENDAR = "stock.trade_calendar"
T_DAILY = "stock.daily"
T_ADJ_FACTOR = "stock.adj_factor"
T_DAILY_BASIC = "stock.daily_basic"
T_SYNC_META = "stock.sync_meta"
T_TOP10_HOLDERS = "stock.top10_holders"


def init_schema(pg_client) -> None:
    """Run all table-creation DDL"""
    for ddl in ALL_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()

# ============================================================
