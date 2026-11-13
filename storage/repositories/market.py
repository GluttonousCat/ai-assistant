"""Market persistence: one stock per transaction, idempotent and retryable."""

from __future__ import annotations

import pandas as pd
from psycopg2.extras import execute_values
from storage.pg import PgClient, _normalize_row


def latest_daily_date():
    with PgClient(read_only=True) as pg:
        row = pg.fetch_one("SELECT max(trade_date) AS latest FROM stock.daily")
        return row["latest"] if row else None


def save_memberships(frame: pd.DataFrame) -> None:
    with PgClient() as pg:
        pg.upsert_df(
            frame,
            "stock",
            "index_membership_snapshot",
            ["index_code", "as_of_date", "ts_code", "source"],
        )


def load_universe() -> list[str]:
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all("""
            SELECT DISTINCT ts_code FROM stock.index_membership_snapshot m
            WHERE as_of_date=(SELECT max(as_of_date) FROM stock.index_membership_snapshot s
                              WHERE s.index_code=m.index_code AND s.source=m.source)
            ORDER BY ts_code
        """)
    return [row["ts_code"] for row in rows]


def save_listed_universe(frame, day):
    """Replace only the current daily snapshot atomically; preserve index history."""
    f = frame.copy()
    f["snapshot_date"] = day
    with PgClient() as pg:
        pg.execute(
            "DELETE FROM stock.listed_universe_snapshot WHERE snapshot_date=%s", (day,)
        )
        pg.upsert_df(
            f, "stock", "listed_universe_snapshot", ["snapshot_date", "ts_code"]
        )


def listed_universe():
    with PgClient(read_only=True) as pg:
        return pg.fetch_all("""SELECT * FROM stock.listed_universe_snapshot
            WHERE snapshot_date=(SELECT max(snapshot_date) FROM stock.listed_universe_snapshot)
            ORDER BY ts_code""")


def reference_gaps(codes, start, end):
    """Per-symbol gaps; a market-wide date watermark cannot prove completeness."""
    with PgClient(read_only=True) as pg:
        return pg.fetch_all(
            """SELECT b.ts_code,
            count(DISTINCT b.bar_end::date) FILTER (WHERE d.ts_code IS NULL) AS daily_missing,
            array_agg(DISTINCT b.bar_end::date) FILTER (WHERE d.ts_code IS NULL) AS daily_dates,
            count(DISTINCT b.bar_end::date) FILTER (WHERE a.ts_code IS NULL) AS factor_missing
            FROM stock.intraday_bar b
            LEFT JOIN stock.daily d ON d.ts_code=b.ts_code AND d.trade_date=b.bar_end::date
            LEFT JOIN stock.adj_factor a ON a.ts_code=b.ts_code AND a.trade_date=b.bar_end::date
            WHERE b.ts_code=ANY(%s) AND b.source='akshare-sina' AND b.adjustment='none'
            AND b.interval_minutes=60 AND b.bar_end>=%s::date AND b.bar_end<%s::date+1
            GROUP BY b.ts_code
            HAVING count(*) FILTER (WHERE d.ts_code IS NULL OR a.ts_code IS NULL)>0
            ORDER BY b.ts_code""",
            (codes, start, end),
        )


def listed_audit(codes, start, end):
    with PgClient(read_only=True) as pg:
        counts = pg.fetch_one(
            """SELECT count(*) AS rows,count(DISTINCT ts_code) AS stocks,
            min(bar_end) AS first_bar,max(bar_end) AS last_bar
            FROM stock.intraday_bar WHERE ts_code=ANY(%s) AND interval_minutes=60
            AND source='akshare-sina' AND adjustment='none'
            AND bar_end>=%s::date AND bar_end<%s::date+1""",
            (codes, start, end),
        )
        gaps = pg.fetch_all(
            """SELECT d.ts_code,count(*) AS missing_days,
            array_agg(d.trade_date ORDER BY d.trade_date) AS missing_dates FROM stock.daily d
            LEFT JOIN (SELECT ts_code,bar_end::date AS day,count(*) AS n FROM stock.intraday_bar
              WHERE source='akshare-sina' AND adjustment='none' AND interval_minutes=60
              AND ts_code=ANY(%s) AND bar_end>=%s::date AND bar_end<%s::date+1
              GROUP BY ts_code,bar_end::date) b ON d.ts_code=b.ts_code AND d.trade_date=b.day
            WHERE d.ts_code=ANY(%s) AND d.trade_date BETWEEN %s AND %s
            AND d.vol>0 AND coalesce(b.n,0)<>4 GROUP BY d.ts_code ORDER BY d.ts_code""",
            (codes, start, end, codes, start, end),
        )
        grid = pg.fetch_one(
            """SELECT count(*) AS invalid FROM stock.intraday_bar
            WHERE ts_code=ANY(%s) AND source='akshare-sina' AND adjustment='none'
            AND interval_minutes=60 AND bar_end>=%s::date AND bar_end<%s::date+1
            AND bar_end::time NOT IN ('10:30','11:30','14:00','15:00')""",
            (codes, start, end),
        )
    return {
        "counts": counts,
        "gaps_against_daily": gaps,
        "reference_gaps": reference_gaps(codes, start, end),
        "invalid_grid": grid["invalid"],
    }


def flag_intraday_gaps(rows, source, start, end):
    """Keep fetched bars, but do not resume past a known incomplete symbol."""
    with PgClient() as pg:
        for row in rows:
            pg.execute(
                """UPDATE stock.intraday_sync_state SET status='error',
                error=%s,updated_at=now() WHERE ts_code=%s AND source=%s
                AND interval_minutes=60 AND adjustment='none'
                AND requested_start=%s AND requested_end=%s""",
                (
                    f"原生60F缺根待核对: {row.get('missing_dates')}",
                    row["ts_code"],
                    source,
                    start,
                    end,
                ),
            )


def completed_codes(source: str, period: int, start: str, end: str) -> set[str]:
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            """SELECT ts_code FROM stock.intraday_sync_state
            WHERE source=%s AND interval_minutes=%s AND adjustment='none'
            AND requested_start=%s AND requested_end=%s AND status='ok'""",
            (source, period, start, end),
        )
    return {r["ts_code"] for r in rows}


def save_result(
    code: str,
    period: int,
    source: str,
    start: str,
    end: str,
    frame: pd.DataFrame | None,
    error: str | None = None,
) -> dict:
    status = "error" if error else "empty" if frame is None or frame.empty else "ok"
    rows = 0 if frame is None else len(frame)
    first = frame.bar_end.min().to_pydatetime() if rows else None
    last = frame.bar_end.max().to_pydatetime() if rows else None
    with PgClient() as pg:
        if rows:
            cols = [
                "ts_code",
                "bar_end",
                "interval_minutes",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "source",
                "adjustment",
            ]
            values = [
                _normalize_row(tuple(r))
                for r in frame[cols].itertuples(index=False, name=None)
            ]
            execute_values(
                pg.cur,
                """INSERT INTO stock.intraday_bar
                (ts_code,bar_end,interval_minutes,open,high,low,close,volume,amount,source,adjustment)
                VALUES %s ON CONFLICT (ts_code,interval_minutes,bar_end,source,adjustment)
                DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                    close=EXCLUDED.close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,fetched_at=now()""",
                values,
                page_size=1000,
            )
        pg.execute(
            """INSERT INTO stock.intraday_sync_state
            (ts_code,interval_minutes,source,adjustment,requested_start,requested_end,status,row_count,first_bar,last_bar,error)
            VALUES (%s,%s,%s,'none',%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ts_code,interval_minutes,source,adjustment,requested_start,requested_end)
            DO UPDATE SET status=EXCLUDED.status,row_count=EXCLUDED.row_count,first_bar=EXCLUDED.first_bar,
                last_bar=EXCLUDED.last_bar,error=EXCLUDED.error,updated_at=now()""",
            (code, period, source, start, end, status, rows, first, last, error),
        )
    return {"ts_code": code, "status": status, "rows": rows}


def read_bars(
    code: str, start: str, end: str, period: int = 60, limit: int = 1000
) -> pd.DataFrame:
    with PgClient(read_only=True) as pg:
        return pg.fetch_df(
            """SELECT ts_code,bar_end,interval_minutes,open,high,low,close,volume,amount,source,adjustment
            FROM stock.intraday_bar WHERE ts_code=%s AND interval_minutes=%s AND adjustment='none'
            AND source='akshare-sina' AND bar_end >= %s::date AND bar_end < %s::date+1
            ORDER BY bar_end DESC LIMIT %s""",
            (code, period, start, end, min(max(limit, 1), 1000)),
        )


def audit(source: str, period: int, start: str, end: str) -> dict:
    with PgClient(read_only=True) as pg:
        counts = pg.fetch_one(
            """SELECT count(*) rows,count(DISTINCT ts_code) stocks,
            min(bar_end) first_bar,max(bar_end) last_bar FROM stock.intraday_bar
            WHERE source=%s AND interval_minutes=%s AND adjustment='none'
            AND bar_end >= %s::date AND bar_end < %s::date+1""",
            (source, period, start, end),
        )
        states = pg.fetch_all(
            """SELECT status,count(*) stocks,sum(row_count) rows FROM stock.intraday_sync_state
            WHERE source=%s AND interval_minutes=%s AND requested_start=%s AND requested_end=%s
            GROUP BY status ORDER BY status""",
            (source, period, start, end),
        )
        gaps = pg.fetch_all(
            """WITH actual AS (
            SELECT ts_code,bar_end::date AS trade_day,count(*) n FROM stock.intraday_bar
            WHERE source=%s AND interval_minutes=%s AND adjustment='none'
            GROUP BY ts_code,bar_end::date)
            SELECT d.ts_code,count(*) missing_days FROM stock.daily d
            JOIN (SELECT DISTINCT ts_code FROM stock.index_membership_snapshot) u USING(ts_code)
            LEFT JOIN actual a ON a.ts_code=d.ts_code AND a.trade_day=d.trade_date
            WHERE d.trade_date BETWEEN %s AND %s AND d.vol > 0 AND coalesce(a.n,0) < 4
            GROUP BY d.ts_code ORDER BY missing_days DESC,d.ts_code""",
            (source, period, start, end),
        )
        daily_end = pg.fetch_one("SELECT max(trade_date) AS trade_day FROM stock.daily")
        no_daily = pg.fetch_all(
            """SELECT DISTINCT m.ts_code FROM stock.index_membership_snapshot m
            WHERE NOT EXISTS (SELECT 1 FROM stock.daily d WHERE d.ts_code=m.ts_code
                              AND d.trade_date BETWEEN %s AND %s) ORDER BY m.ts_code""",
            (start, end),
        )
        calendar = pg.fetch_one(
            """SELECT count(DISTINCT cal_date) trading_days,max(cal_date) last_trade_date FROM stock.trade_calendar
            WHERE cal_date BETWEEN %s AND %s AND is_open=1""",
            (start, end),
        )
        latest_absent = pg.fetch_all(
            """SELECT ts_code,max(bar_end) last_bar FROM stock.intraday_bar
            WHERE source=%s AND interval_minutes=%s AND adjustment='none'
            GROUP BY ts_code HAVING max(bar_end)::date < %s ORDER BY ts_code""",
            (source, period, calendar["last_trade_date"]),
        )
        invalid_grid = pg.fetch_all(
            """SELECT ts_code,bar_end FROM stock.intraday_bar
            WHERE source=%s AND interval_minutes=60 AND adjustment='none'
            AND bar_end >= %s::date AND bar_end < %s::date+1
            AND bar_end::time NOT IN ('10:30','11:30','14:00','15:00') LIMIT 20""",
            (source, start, end),
        )
    return {
        "counts": counts,
        "states": states,
        "daily_reference_end": daily_end["trade_day"],
        "gaps_against_daily": gaps,
        "symbols_without_daily_reference": [r["ts_code"] for r in no_daily],
        "trading_days": calendar["trading_days"],
        "last_trade_date": calendar["last_trade_date"],
        "latest_day_absent": latest_absent,
        "invalid_60m_grid_samples": invalid_grid,
    }
