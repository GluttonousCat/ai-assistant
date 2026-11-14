"""SQL boundary for trend inputs and atomically published scan results."""

from datetime import datetime
import json

from psycopg2.extras import Json, execute_values
from storage.pg import PgClient


def cutoffs(now: datetime) -> dict:
    with PgClient(read_only=True) as pg:
        bars = pg.fetch_one(
            """SELECT max(bar_end) AS bar_end FROM stock.intraday_bar
            WHERE source='akshare-sina' AND adjustment='none' AND interval_minutes=60 AND bar_end<=%s""",
            (now,),
        )
        factor = pg.fetch_one(
            "SELECT max(trade_date) AS factor_date FROM stock.adj_factor"
        )
        daily = pg.fetch_one(
            "SELECT max(trade_date) AS daily_end FROM stock.daily WHERE trade_date::timestamp+interval '15 hours'<=%s",
            (now,),
        )
    return {**bars, **factor, **daily}


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
        return pg.fetch_all(
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
        )


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


def latest_snapshot(run_id: str | None = None, include_rows: bool = True):
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
        rows = (
            pg.fetch_all(
                "SELECT payload FROM stock.trend_signal WHERE run_id=%s ORDER BY ts_code",
                (run["run_id"],),
            )
            if include_rows
            else []
        )
    return json.loads(
        json.dumps({**run, "data": [r["payload"] for r in rows]}, default=str)
    )
