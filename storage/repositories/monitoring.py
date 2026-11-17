"""Database observations for the administration monitor."""

from storage.pg import PgClient


def database_state():
    with PgClient(read_only=True) as pg:
        pipeline = pg.fetch_one(
            "SELECT count(*) total,"
            " count(*) FILTER (WHERE analysis_status='done') done,"
            " count(*) FILTER (WHERE analysis_status='failed') failed,"
            " count(*) FILTER (WHERE extraction_status='pending_ocr') ocr,"
            " count(*) FILTER (WHERE content_text_cn IS NOT NULL) translated"
            " FROM fin.report_meta"
        )
        freshness = pg.fetch_all(
            "SELECT table_name, updated_at FROM fin.sync_meta"
            " ORDER BY updated_at DESC LIMIT 8"
        )
        quote = pg.fetch_one("SELECT max(trade_date)::text d FROM stock.daily")
    return {
        "pipeline": dict(pipeline) if pipeline else {},
        "freshness": [dict(row) for row in freshness],
        "last_trade_date": quote["d"] if quote else None,
    }
