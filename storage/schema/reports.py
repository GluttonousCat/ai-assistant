"""Reports schema definitions; SQL moved without changing table names."""
from core.logger import get_logger
from storage.schema.connection import ddl_cursor
logger = get_logger(__name__)

# Report data warehouse (schema: fin) — PRD 2.2
# ============================================================
# Sources: zsxq crawler (topic attachments: PDF/DOCX/TXT) + manual upload
# Purpose: data foundation for F4 report consistency checks / F5 analysis report generation

DDL_REPORT_META = """
CREATE TABLE IF NOT EXISTS fin.report_meta (
    report_id       SERIAL PRIMARY KEY,
    topic_id        BIGINT,                    -- zsxq topic ID (source identifier)
    file_id         BIGINT,                    -- zsxq file ID
    ts_code         VARCHAR(16),               -- target stock (inferred from text, nullable)
    title           VARCHAR(256),              -- file title
    author          VARCHAR(64),               -- analyst
    org_name        VARCHAR(64),               -- broker name
    publish_date    DATE,                      -- publish date
    report_type     VARCHAR(16),               -- report type (in-depth / flash note / quarterly)
    source          VARCHAR(32) DEFAULT 'zsxq',-- source (zsxq/upload)
    file_path       TEXT,                      -- local file path
    file_name       VARCHAR(256),              -- original file name
    file_size       BIGINT,                    -- file size (bytes)
    content_text    TEXT,                      -- extracted text content
    content_chars   INTEGER DEFAULT 0,         -- text length (quality check)
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
    ts_code         VARCHAR(16),               -- target code (nullable: non-A-share reports have no ts_code; NOT NULL in legacy DBs relaxed by ensure)
    forecast_type   VARCHAR(32),               -- forecast type: revenue/net_profit/eps/target_price
    forecast_period VARCHAR(16),               -- forecast period: 2024/2025E
    forecast_value  NUMERIC(24,4),             -- forecast value
    forecast_unit   VARCHAR(16),               -- unit: 100M CNY / CNY
    confidence      NUMERIC(5,2),              -- extraction confidence 0-100
    raw_text        TEXT,                      -- original text snippet
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

# Dedup-merge deletion trail: sync idempotency relies on file_id existence checks, so a
# physically deleted file_id vanishes and _sync_group_files (full scan) would re-insert
# it -> an ingest/delete/re-insert/re-analyze ping-pong loop (this once made the same
# image-only PDF get OCR'd repeatedly, burning qwen). Record the deletion here before
# removing; sync checks this table and never re-inserts.
DDL_REPORT_MERGED = """
CREATE TABLE IF NOT EXISTS fin.report_meta_merged (
    file_id     BIGINT PRIMARY KEY,            -- file ID of the merged-away duplicate
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
    """Deploy the report-related tables (idempotent)"""
    for ddl in REPORT_DDL:
        pg_client.execute(ddl)
    pg_client.conn.commit()


# (the v_report_ready view was dropped 2026-10-11: zero query consumers in the whole DB,
#  absent from the LLM data dictionary, pure legacy;
#  equivalent query when needed: SELECT ... FROM fin.report_meta WHERE extraction_status='extracted'
#  AND ts_code IS NOT NULL AND content_chars > 0)


# ============================================================
# Report table incremental columns — single source of truth
# ============================================================
# The extension columns/indexes of report_meta / report_forecast were once scattered
# across two ensure sites: tools/finance/report_meta_analysis.py (filename-inference
# columns) and skills/report/skill.py (LLM deep-extraction columns); refactored and
# consolidated here. Both modules' ensure_* calls merely delegate.


DDL_REPORT_ANALYSIS_STATEMENTS = [
    # (statement, table, column or index name): execute only when the column/index is
    # missing (idempotent + avoids pointless ALTER locks)
    # Filename-inference columns
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
    # LLM deep-extraction columns (analysis_status values: none/done/failed/no_llm)
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
    # Chinese translation of the body (written by a background translator when an
    # English original is ingested; left empty for Chinese bodies, readers fall back
    # to the original text)
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



def ensure_report_analysis_columns() -> None:
    """Add missing report incremental columns/indexes, and relax legacy report_forecast.ts_code to nullable (idempotent, once per process).

    The base DDL already defines ts_code as nullable; legacy DBs were created with
    NOT NULL, and non-A-share reports have no ts_code — relaxed here as a fallback.
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
