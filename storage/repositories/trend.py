"""SQL boundary for trend inputs and atomically published scan results."""

from datetime import datetime

from psycopg2.extras import Json, execute_values
from storage.pg import PgClient
from storage.schema.trend import TRACKING_SCORE_PREDICATE


def cutoffs(now: datetime) -> dict:
    with PgClient(read_only=True) as pg:
        return pg.fetch_one(
            """SELECT
                (SELECT max(bar_end) FROM stock.intraday_bar
                 WHERE source='akshare-sina' AND adjustment='none' AND interval_minutes=60
                 AND bar_end<=%s) AS bar_end,
                (SELECT max(trade_date) FROM stock.adj_factor) AS factor_date,
                (SELECT max(trade_date) FROM stock.daily
                 WHERE trade_date <= (%s::timestamp - interval '15 hours')::date) AS daily_end""",
            (now, now),
        )


def digest_stock_context(codes, trade_date):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            """SELECT u.ts_code, NULLIF(trim(s.industry_l2), '') AS industry_l2,
                      b.total_mv, b.pe_ttm, b.pb, d.pct_chg
               FROM unnest(%s::text[]) AS u(ts_code)
               LEFT JOIN stock.stock_basic s USING (ts_code)
               LEFT JOIN stock.daily_basic b ON b.ts_code=u.ts_code AND b.trade_date=%s
               LEFT JOIN stock.daily d ON d.ts_code=u.ts_code AND d.trade_date=%s""",
            (codes, trade_date, trade_date),
        )
    return {row["ts_code"]: row for row in rows}


def universe(as_of: str, listed=False) -> list[dict]:
    with PgClient(read_only=True) as pg:
        if listed and pg.table_exists("stock", "listed_universe_snapshot"):
            # This is a current observed universe, not reconstructed historical
            # membership. Allow an earlier quote cutoff, recording fetched_at.
            rows = pg.fetch_df_stream("""WITH dates AS (
                SELECT index_code,max(as_of_date) AS as_of_date
                FROM stock.index_membership_snapshot GROUP BY index_code
            ), membership AS (
                SELECT m.ts_code,array_agg(DISTINCT m.index_code::text ORDER BY m.index_code::text) AS indices
                FROM stock.index_membership_snapshot m JOIN dates d USING(index_code,as_of_date)
                GROUP BY m.ts_code
            ) SELECT u.ts_code,u.name,u.industry,u.board,
                u.snapshot_date AS membership_date,u.fetched_at AS observed_at,
                coalesce(m.indices,ARRAY[]::text[]) AS indices
                FROM stock.listed_universe_snapshot u
                LEFT JOIN membership m USING(ts_code)
                WHERE u.snapshot_date=(SELECT max(snapshot_date) FROM stock.listed_universe_snapshot)
                ORDER BY u.ts_code""").to_dict(orient="records")
            if rows:
                return rows
        return pg.fetch_df_stream(
            """WITH dates AS (
            SELECT index_code,max(as_of_date) AS as_of_date FROM stock.index_membership_snapshot
            WHERE as_of_date<=%s AND source='tushare' GROUP BY index_code)
            SELECT m.ts_code,coalesce(b.name,m.ts_code) AS name,coalesce(b.industry_l1,b.industry,'未分类') AS industry,
                array_agg(m.index_code ORDER BY m.index_code) AS indices,
                min(m.as_of_date) AS membership_date
            FROM stock.index_membership_snapshot m JOIN dates d USING(index_code,as_of_date)
            LEFT JOIN stock.stock_basic b USING(ts_code) WHERE m.source='tushare'
            GROUP BY m.ts_code,b.name,b.industry_l1,b.industry ORDER BY m.ts_code""",
            (as_of,),
        ).to_dict(orient="records")


def inputs(codes: list[str], start: str, end: str):
    with PgClient(read_only=True) as pg:
        return pg.fetch_df_stream(
            """SELECT b.ts_code,b.bar_end,b.open,b.high,b.low,b.close,b.volume,b.amount,f.adj_factor
            FROM stock.intraday_bar b LEFT JOIN stock.adj_factor f
            ON f.ts_code=b.ts_code AND f.trade_date=b.bar_end::date
            WHERE b.ts_code=ANY(%s) AND b.interval_minutes=60 AND b.source='akshare-sina' AND b.adjustment='none'
            AND b.bar_end>=%s::date AND b.bar_end<=%s::timestamp ORDER BY b.ts_code,b.bar_end""",
            (codes, start, end),
        )


def daily_inputs(codes: list[str], start: str, end: str):
    """Native daily bars; Tushare lots/thousand CNY become shares/CNY."""
    with PgClient(read_only=True) as pg:
        return pg.fetch_df_stream(
            """SELECT d.ts_code,d.trade_date::timestamp+interval '15 hours' AS bar_end,
            d.open,d.high,d.low,d.close,d.vol*100 AS volume,d.amount*1000 AS amount,f.adj_factor
            FROM stock.daily d LEFT JOIN stock.adj_factor f USING(ts_code,trade_date)
            WHERE d.ts_code=ANY(%s) AND d.trade_date>=%s::date AND d.trade_date::timestamp+interval '15 hours'<=%s::timestamp
            AND d.vol>0 ORDER BY d.ts_code,d.trade_date""",
            (codes, start, end),
        )


def missing_adjustments(start: str, end: str):
    with PgClient(read_only=True) as pg:
        return pg.fetch_all(
            """SELECT b.ts_code,array_agg(DISTINCT b.bar_end::date ORDER BY b.bar_end::date) AS dates
            FROM stock.intraday_bar b LEFT JOIN stock.adj_factor f
            ON f.ts_code=b.ts_code AND f.trade_date=b.bar_end::date
            WHERE b.interval_minutes=60 AND b.source='akshare-sina' AND b.adjustment='none'
            AND b.bar_end>=%s::date AND b.bar_end<%s::date+1 AND f.adj_factor IS NULL
            GROUP BY b.ts_code ORDER BY b.ts_code""",
            (start, end),
        )


def missing_daily_adjustments(start: str, end: str):
    with PgClient(read_only=True) as pg:
        return pg.fetch_all(
            """SELECT d.ts_code,array_agg(d.trade_date ORDER BY d.trade_date) AS dates
            FROM stock.daily d LEFT JOIN stock.adj_factor f USING(ts_code,trade_date)
            WHERE d.trade_date BETWEEN %s::date AND %s::date AND d.vol>0 AND f.adj_factor IS NULL
            GROUP BY d.ts_code ORDER BY d.ts_code""", (start, end))


def save_adjustments(frame):
    with PgClient() as pg:
        pg.upsert_df(frame, "stock", "adj_factor", ["ts_code", "trade_date"])


def publish(run_id: str, as_of: str, version: str, metadata: dict, rows: list[dict]):
    with PgClient() as pg:
        pg.execute(
            "INSERT INTO stock.trend_scan(run_id,as_of,version,metadata) VALUES (%s,%s,%s,%s)",
            (run_id, as_of, version, Json(metadata)),
        )
        execute_values(
            pg.cur,
            "INSERT INTO stock.trend_signal(run_id,ts_code,payload) VALUES %s",
            [(run_id, r["ts_code"], Json(r)) for r in rows],
            page_size=500,
        )


def latest_snapshot(run_id: str | None = None, include_rows: bool = True, *,
                    min_numeric_score: int | None = None, code: str | None = None):
    with PgClient(read_only=True) as pg:
        # Missing schema is a friendly first-run state, without request-time DDL.
        if not pg.fetch_one("SELECT to_regclass('stock.trend_scan') AS name")["name"]:
            return None
        run = pg.fetch_one(
            """SELECT run_id::text,created_at,as_of,version,metadata FROM stock.trend_scan
            WHERE (%s::uuid IS NULL OR run_id=%s::uuid) ORDER BY created_at DESC LIMIT 1""",
            (run_id, run_id),
        )
        if run is None:
            return None
        rows = []
        if include_rows:
            sql = "SELECT payload FROM stock.trend_signal WHERE run_id=%s"
            params = [run["run_id"]]
            if code is not None:
                sql += " AND ts_code=%s"
                params.append(code)
            if min_numeric_score is not None:
                # Historical strings and missing scores still go through the caller's rules.
                sql += """ AND (jsonb_typeof(payload->'score') IS DISTINCT FROM 'number'
                             OR payload->'score' >= to_jsonb(%s::int))"""
                params.append(min_numeric_score)
            rows = pg.fetch_all(sql + " ORDER BY ts_code", params, as_dict=False)
    return {**_public_run(run), "data": [payload for (payload,) in rows]}


def _public_run(run):
    # JSONB metadata and payloads are already decoded. Only these SQL timestamps
    # need the historical wire format (space separator and session UTC offset).
    return {**run, "as_of": str(run["as_of"]), "created_at": str(run["created_at"])}


def history_days(end: str, limit: int = 120):
    """One published snapshot per signal date, retaining its original version."""
    with PgClient(read_only=True) as pg:
        if not pg.fetch_one("SELECT to_regclass('stock.trend_scan') AS name")["name"]:
            return []
        rows = pg.fetch_all(
            """SELECT DISTINCT ON (as_of::date) run_id::text,as_of,created_at,version
            FROM stock.trend_scan WHERE as_of<=%s::timestamp
            ORDER BY as_of::date DESC,as_of DESC,created_at DESC,run_id DESC LIMIT %s""",
            (end, limit),
        )
    return [_public_run(row) for row in rows]


def trading_days(start: str, end: str):
    with PgClient(read_only=True) as pg:
        return [str(r["cal_date"]) for r in pg.fetch_all(
            """SELECT cal_date FROM stock.trade_calendar WHERE exchange='SSE'
            AND is_open=1 AND cal_date>=%s::date AND cal_date<=%s::date ORDER BY cal_date""",
            (start, end),
        )]


def tracking_history(end: str, limit=120):
    """Bounded historical candidates in two reads, including empty scan days."""
    with PgClient(read_only=True) as pg:
        if not pg.fetch_one("SELECT to_regclass('stock.trend_scan') AS name")["name"]:
            return []
        runs = pg.fetch_all(
            """SELECT DISTINCT ON (as_of::date) run_id::text,as_of,created_at,version,metadata
            FROM stock.trend_scan WHERE as_of<=%s::timestamp
            ORDER BY as_of::date DESC,as_of DESC,created_at DESC,run_id DESC LIMIT %s""", (end, limit))
        if not runs:
            return []
        rows = pg.fetch_all(
            f"""SELECT run_id::text,payload FROM stock.trend_signal
            WHERE run_id=ANY(%s::uuid[]) AND {TRACKING_SCORE_PREDICATE}
            ORDER BY run_id,ts_code""", ([r["run_id"] for r in runs],))
    grouped = {r["run_id"]: [] for r in runs}
    for row in rows:
        grouped[row["run_id"]].append(row["payload"])
    return [{**_public_run(r), "data": grouped[r["run_id"]]} for r in runs]
