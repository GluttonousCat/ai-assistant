"""Versioned scan snapshots, published only after the entire scan succeeds."""

from storage.schema.connection import create_indexes_concurrently, ddl_cursor


TRACKING_SCORE_PREDICATE = """CASE WHEN (payload->>'score') ~ '^[0-9]+([.][0-9]+)?$'
    THEN (payload->>'score')::numeric >= 60 ELSE false END"""


def deploy_tracking_index():
    """Run once after the trend tables exist. Reads and publication also work without it."""
    create_indexes_concurrently("stock", {
        "idx_trend_signal_tracking": f"ON stock.trend_signal (run_id, ts_code) WHERE {TRACKING_SCORE_PREDICATE}",
    })


def ensure_trend_schema():
    with ddl_cursor() as cur:
        cur.execute("SET lock_timeout='5s'")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock.trend_scan (
                run_id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                as_of TIMESTAMP NOT NULL,
                version VARCHAR(48) NOT NULL,
                metadata JSONB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stock.trend_signal (
                run_id UUID NOT NULL REFERENCES stock.trend_scan(run_id) ON DELETE CASCADE,
                ts_code VARCHAR(16) NOT NULL,
                payload JSONB NOT NULL,
                PRIMARY KEY (run_id, ts_code)
            );
            COMMENT ON TABLE stock.trend_scan IS '60F趋势研究快照; 参数/数据截止/成分日期随版本冻结';
            COMMENT ON TABLE stock.trend_signal IS '全池筛选解释; eligible仅表示规则通过,不是交易指令';
        """)
