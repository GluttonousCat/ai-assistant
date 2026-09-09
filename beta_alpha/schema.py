"""
beta_alpha 模块自有 DDL (fin.chain_extract 研报环节抽取表)

按项目约定「模块内 ensure 函数」存放; fin.fina_mainbz 属于 Tushare 同步族,
其 DDL 仍在 storage/pg_schema.py。
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
    """执行链图谱建表 DDL (幂等; 独立连接/事务外调用)"""
    pg_client.execute(DDL_CHAIN_EXTRACT)
    pg_client.conn.commit()
