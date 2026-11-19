"""Market ingestion schema. All DDL uses a separate autocommit connection."""
from storage.schema.connection import ddl_cursor

DDL_INDEX_DAILY = """
CREATE TABLE IF NOT EXISTS stock.index_daily (
    trade_date DATE NOT NULL,
    ts_code VARCHAR(16) NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    pct_chg DOUBLE PRECISION,
    vol DOUBLE PRECISION,
    amount DOUBLE PRECISION,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_index_daily_code ON stock.index_daily (ts_code, trade_date);
"""

_CUTOFF_INDEXES = {
    "idx_intraday_bar_60f_native_end": """ON stock.intraday_bar (bar_end)
        WHERE interval_minutes=60 AND source='akshare-sina' AND adjustment='none'""",
    "idx_adj_factor_trade_date": "ON stock.adj_factor (trade_date)",
    "idx_daily_price_limit_trade_date": "ON stock.daily_price_limit (trade_date)",
}

DDL = """
CREATE TABLE IF NOT EXISTS stock.listed_universe_snapshot (
    snapshot_date DATE NOT NULL,
    ts_code VARCHAR(16) NOT NULL,
    name TEXT NOT NULL,
    industry TEXT NOT NULL,
    board VARCHAR(16) NOT NULL,
    list_date DATE,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(snapshot_date,ts_code)
);
CREATE TABLE IF NOT EXISTS stock.index_membership_snapshot (
    index_code VARCHAR(16) NOT NULL,
    as_of_date DATE NOT NULL,
    ts_code VARCHAR(16) NOT NULL,
    weight NUMERIC(16,8),
    source VARCHAR(32) NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (index_code, as_of_date, ts_code, source)
);
CREATE TABLE IF NOT EXISTS stock.intraday_bar (
    ts_code VARCHAR(16) NOT NULL,
    bar_end TIMESTAMP NOT NULL,
    interval_minutes SMALLINT NOT NULL CHECK (interval_minutes IN (5,15,30,60)),
    open NUMERIC(18,6) NOT NULL,
    high NUMERIC(18,6) NOT NULL,
    low NUMERIC(18,6) NOT NULL,
    close NUMERIC(18,6) NOT NULL,
    volume NUMERIC(24,4) NOT NULL CHECK (volume >= 0),
    amount NUMERIC(28,6),
    source VARCHAR(32) NOT NULL,
    adjustment VARCHAR(16) NOT NULL CHECK (adjustment IN ('none','forward','backward')),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ts_code, interval_minutes, bar_end, source, adjustment),
    CHECK (high >= low AND high >= open AND high >= close AND low <= open AND low <= close)
);
CREATE TABLE IF NOT EXISTS stock.intraday_sync_state (
    ts_code VARCHAR(16) NOT NULL,
    interval_minutes SMALLINT NOT NULL,
    source VARCHAR(32) NOT NULL,
    adjustment VARCHAR(16) NOT NULL,
    requested_start DATE NOT NULL,
    requested_end DATE NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('ok','empty','error')),
    row_count INTEGER NOT NULL DEFAULT 0,
    first_bar TIMESTAMP,
    last_bar TIMESTAMP,
    error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ts_code, interval_minutes, source, adjustment, requested_start, requested_end)
);
COMMENT ON TABLE stock.intraday_bar IS '原生分钟K线; bar_end为Asia/Shanghai结束时间; volume股,amount元; 多源及复权隔离';
COMMENT ON TABLE stock.listed_universe_snapshot IS '采集当日非ST上市名单与板块; fetched_at为真实获知时间; 不是历史指数成员或逐日ST状态';
COMMENT ON TABLE stock.index_membership_snapshot IS '供应商成分权重日期快照; 不推定为历史调入调出日期';
"""


def ensure_intraday_schema() -> None:
    with ddl_cursor() as cur:
        cur.execute("SET lock_timeout='5s'")
        cur.execute("CREATE SCHEMA IF NOT EXISTS stock")
        cur.execute(DDL)


def ensure_index_daily_schema() -> None:
    with ddl_cursor() as cur:
        cur.execute("SET lock_timeout='5s'")
        cur.execute("SELECT to_regclass('stock.index_daily'), to_regclass('stock.idx_index_daily_code')")
        if not all(cur.fetchone()):
            cur.execute("CREATE SCHEMA IF NOT EXISTS stock")
            cur.execute(DDL_INDEX_DAILY)


def deploy_cutoff_indexes() -> None:
    """Run explicitly after the market tables exist, outside application startup."""
    with ddl_cursor() as cur:
        cur.execute("SET lock_timeout='5s'")
        cur.execute("SET statement_timeout='10min'")
        for name, definition in _CUTOFF_INDEXES.items():
            cur.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")
            cur.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)", (f"stock.{name}",))
            if cur.fetchone() != (True,):
                raise RuntimeError(f"行情索引 stock.{name} 无效，请检查后重建")
