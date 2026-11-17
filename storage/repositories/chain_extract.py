"""Report-to-chain evidence persistence with isolated schema deployment."""

from storage.pg import PgClient
from storage.schema.industry import DDL_CHAIN_EXTRACT, ensure_chain_extract_schema


def init_chain_extract_schema(pg_client=None):
    # The argument remains accepted for legacy callers; DDL owns its connection.
    ensure_chain_extract_schema()


def extraction_text(report_id):
    ensure_chain_extract_schema()
    with PgClient(read_only=True) as pg:
        return pg.fetch_one(
            "SELECT content_text FROM fin.report_meta WHERE report_id=%s", (report_id,)
        )


def replace_segments(report_id, rows):
    with PgClient() as pg:
        pg.execute("DELETE FROM fin.chain_extract WHERE report_id=%s", (report_id,))
        if rows:
            pg.executemany(
                "INSERT INTO fin.chain_extract (report_id, segment, direction, companies, confidence) VALUES (%s, %s, %s, %s, %s)",
                [
                    (
                        report_id,
                        r["segment"],
                        r["direction"],
                        r["companies"],
                        r["confidence"],
                    )
                    for r in rows
                ],
            )
