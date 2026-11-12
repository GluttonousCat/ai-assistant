"""Industry schema definitions; SQL moved without changing table names."""
# SW industry classification (SW2021) + stock-to-industry mapping
# ============================================================

DDL_INDEX_CLASSIFY = """
CREATE TABLE IF NOT EXISTS stock.index_classify (
    index_code     VARCHAR(16) PRIMARY KEY,  -- industry index code (801125.SI)
    industry_name  VARCHAR(32) NOT NULL,     -- industry name (e.g. liquor II)
    level          VARCHAR(4),               -- L1/L2/L3
    industry_code  VARCHAR(16),              -- industry code (340500)
    is_pub         SMALLINT,                 -- public industry or not
    parent_code    VARCHAR(16),              -- parent industry code (340000)
    src            VARCHAR(16),              -- source (SW2021)
    upd_date       VARCHAR(16)               -- update date
);
COMMENT ON TABLE stock.index_classify IS '申万行业分类 (SW2021)';
"""

DDL_STOCK_INDUSTRY = """
CREATE TABLE IF NOT EXISTS stock.stock_industry (
    index_code    VARCHAR(16) NOT NULL,   -- level-2 industry index code
    con_code      VARCHAR(16) NOT NULL,   -- constituent ts_code
    in_date       VARCHAR(16),            -- inclusion date (YYYYMMDD)
    out_date      VARCHAR(16),            -- exclusion date (empty = still in)
    is_new        VARCHAR(2),             -- N=no Y=yes (newly included)
    PRIMARY KEY (index_code, con_code)
);
CREATE INDEX IF NOT EXISTS idx_stock_ind_con ON stock.stock_industry (con_code);
COMMENT ON TABLE stock.stock_industry IS '个股 -> 申万二级行业映射';
"""


# ============================================================
# (industry DDL lives above: DDL_INDEX_CLASSIFY + DDL_STOCK_INDUSTRY)
# ============================================================
