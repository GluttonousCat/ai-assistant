"""Research backtests and actual daily price limits; deployed outside requests."""

from storage.schema.connection import ddl_cursor


def ensure_quant_schema():
    with ddl_cursor() as cur:
        cur.execute("SET lock_timeout='5s'")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock.daily_price_limit (
                ts_code VARCHAR(16) NOT NULL,
                trade_date DATE NOT NULL,
                up_limit NUMERIC(16,4),
                down_limit NUMERIC(16,4),
                source VARCHAR(24) NOT NULL DEFAULT 'tushare',
                PRIMARY KEY(ts_code,trade_date)
            );
            CREATE TABLE IF NOT EXISTS stock.backtest_run (
                run_id UUID PRIMARY KEY,
                suite_id UUID NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                strategy_id VARCHAR(48) NOT NULL,
                timeframe VARCHAR(8) NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                universe_mode VARCHAR(24) NOT NULL,
                payload JSONB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS backtest_run_created_idx ON stock.backtest_run(created_at DESC);
            CREATE TABLE IF NOT EXISTS stock.backtest_trade (
                run_id UUID NOT NULL REFERENCES stock.backtest_run(run_id) ON DELETE CASCADE,
                trade_no INTEGER NOT NULL,
                payload JSONB NOT NULL,
                PRIMARY KEY(run_id,trade_no)
            );
            COMMENT ON TABLE stock.backtest_run IS '离线研究回测; 固定样本偏差/成交假设/未平仓随版本冻结';
            COMMENT ON TABLE stock.daily_price_limit IS '供应商历史真实涨跌停价,不按当前简称推测历史限制';
        """)
