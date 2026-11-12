"""
beta_alpha module's own DDL (fin.chain_extract: report chain-segment extraction table)

Stored here per the project convention of "ensure functions inside the module";
fin.fina_mainbz belongs to the Tushare sync family, so its DDL stays in
storage/pg_schema.py.
"""
from __future__ import annotations

DDL_CHAIN_EXTRACT = """
CREATE TABLE IF NOT EXISTS fin.chain_extract (
    id          SERIAL PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES fin.report_meta(report_id) ON DELETE CASCADE,
    segment     VARCHAR(128) NOT NULL,       -- 环节/产品/材料 原文短语
    direction   VARCHAR(16),                 -- upstream/midstream/downstream/other
    companies   TEXT,                        -- 关联公司 (原文表述, 顿号分隔, 最多4个)
    confidence  NUMERIC(4,2),                -- 0-1 实质讨论程度
    created_at  TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chain_extract_segment ON fin.chain_extract (segment);
CREATE INDEX IF NOT EXISTS idx_chain_extract_report ON fin.chain_extract (report_id);
COMMENT ON TABLE fin.chain_extract IS '研报产业链环节抽取 (每篇报告x每环节一行, 幂等重抽)';
"""


def init_chain_extract_schema(pg_client) -> None:
    """Run the chain-graph DDL (idempotent; call outside business transactions, on its own connection)"""
    pg_client.execute(DDL_CHAIN_EXTRACT)
    pg_client.conn.commit()
