"""Quant SQL boundary. No provider calls, calculation, or request-time DDL."""

import json
from psycopg2.extras import Json, execute_values
from storage.pg import PgClient


def input_dates(start, end):
    with PgClient(read_only=True) as pg:
        days = pg.fetch_all(
            "SELECT cal_date FROM stock.trade_calendar WHERE exchange='SSE' AND is_open=1 AND cal_date BETWEEN %s AND %s ORDER BY cal_date",
            (start, end),
        )
        limits = pg.fetch_all(
            "SELECT DISTINCT trade_date FROM stock.daily_price_limit WHERE trade_date BETWEEN %s AND %s",
            (start, end),
        )
        daily = pg.fetch_all(
            "SELECT DISTINCT trade_date FROM stock.daily WHERE trade_date BETWEEN %s AND %s",
            (start, end),
        )
    return (
        [r["cal_date"] for r in days],
        {r["trade_date"] for r in limits},
        {r["trade_date"] for r in daily},
    )


def save_limits(frame):
    with PgClient() as pg:
        return pg.upsert_df(
            frame, "stock", "daily_price_limit", ["ts_code", "trade_date"]
        )


def save_daily(frame):
    with PgClient() as pg:
        return pg.upsert_df(frame, "stock", "daily", ["trade_date", "ts_code"])


def save_index(frame):
    frame = frame[
        [
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pct_chg",
            "vol",
            "amount",
        ]
    ]
    with PgClient() as pg:
        return pg.upsert_df(frame, "stock", "index_daily", ["trade_date", "ts_code"])


def quotes(codes, start, end, timeframe):
    with PgClient(read_only=True) as pg:
        if timeframe == "60F":
            return pg.fetch_df(
                """SELECT b.ts_code,b.bar_end,b.open,b.high,b.low,b.close,b.volume,b.amount,
                f.adj_factor,l.up_limit,l.down_limit
                FROM stock.intraday_bar b LEFT JOIN stock.adj_factor f ON f.ts_code=b.ts_code AND f.trade_date=b.bar_end::date
                LEFT JOIN stock.daily_price_limit l ON l.ts_code=b.ts_code AND l.trade_date=b.bar_end::date
                WHERE b.ts_code=ANY(%s) AND b.interval_minutes=60 AND b.source='akshare-sina' AND b.adjustment='none'
                AND b.bar_end::date BETWEEN %s AND %s ORDER BY b.ts_code,b.bar_end""",
                (codes, start, end),
            )
        return pg.fetch_df(
            """SELECT d.ts_code,d.trade_date::timestamp+interval '15 hours' AS bar_end,
            d.open,d.high,d.low,d.close,d.vol*100 AS volume,d.amount*1000 AS amount,
            f.adj_factor,l.up_limit,l.down_limit FROM stock.daily d
            LEFT JOIN stock.adj_factor f USING(ts_code,trade_date)
            LEFT JOIN stock.daily_price_limit l USING(ts_code,trade_date)
            WHERE d.ts_code=ANY(%s) AND d.trade_date BETWEEN %s AND %s AND d.vol>0
            ORDER BY d.ts_code,d.trade_date""",
            (codes, start, end),
        )


def benchmarks(start, end):
    with PgClient(read_only=True) as pg:
        return pg.fetch_all(
            "SELECT ts_code,trade_date,close FROM stock.index_daily WHERE ts_code IN ('000300.SH','000852.SH') AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
            (start, end),
        )


def observed_dates(codes, end):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            """SELECT ts_code,min(fetched_at AT TIME ZONE 'Asia/Shanghai') AS observed_at
            FROM stock.index_membership_snapshot WHERE ts_code=ANY(%s) AND as_of_date<=%s AND source='tushare' GROUP BY ts_code""",
            (codes, end),
        )
    return {r["ts_code"]: r["observed_at"] for r in rows}


def publish(result):
    payload = {k: v for k, v in result.items() if k != "trades"}
    with PgClient() as pg:
        pg.execute(
            "INSERT INTO stock.backtest_run(run_id,suite_id,strategy_id,timeframe,start_date,end_date,universe_mode,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id) DO NOTHING",
            (
                result["run_id"],
                result["suite_id"],
                result["strategy_id"],
                result["timeframe"],
                result["start"],
                result["end"],
                result["universe_mode"],
                Json(payload),
            ),
        )
        if result["trades"]:
            execute_values(
                pg.cur,
                "INSERT INTO stock.backtest_trade(run_id,trade_no,payload) VALUES %s ON CONFLICT(run_id,trade_no) DO NOTHING",
                [
                    (result["run_id"], i + 1, Json(t))
                    for i, t in enumerate(result["trades"])
                ],
                page_size=500,
            )


def list_runs(limit=50):
    with PgClient(read_only=True) as pg:
        if not pg.fetch_one("SELECT to_regclass('stock.backtest_run') AS name")["name"]:
            return []
        return pg.fetch_all(
            """SELECT run_id::text,suite_id::text,created_at::text,strategy_id,timeframe,
            start_date::text,end_date::text,universe_mode,payload->'metrics' AS metrics,
            payload->'diagnostics' AS diagnostics,payload->>'strategy_version' AS strategy_version,
            payload->>'execution_version' AS execution_version FROM stock.backtest_run ORDER BY created_at DESC,run_id LIMIT %s""",
            (limit,),
        )


def mark_suite_superseded(suite_id, reason):
    """Keep all historical values; explicitly label a research run with a defect."""
    with PgClient() as pg:
        pg.execute(
            """UPDATE stock.backtest_run SET payload=jsonb_set(
            jsonb_set(payload,'{diagnostics,superseded}','true'::jsonb),
            '{diagnostics,superseded_reason}',to_jsonb(%s::text)) WHERE suite_id=%s::uuid""",
            (reason, suite_id),
        )


def annotate_replay_config(suite_id, config):
    """Complete replay parameter provenance without changing any metric/trade."""
    with PgClient() as pg:
        pg.execute(
            "UPDATE stock.backtest_run SET payload=jsonb_set(payload,'{strategy_config}',%s::jsonb) WHERE suite_id=%s::uuid",
            (Json(config), suite_id),
        )


def annotate_diagnostics(run_id, diagnostics):
    """Complete report context without changing returns, equity or trades."""
    with PgClient() as pg:
        pg.execute(
            """UPDATE stock.backtest_run SET payload=jsonb_set(
            payload,'{diagnostics}',coalesce(payload->'diagnostics','{}'::jsonb) || %s::jsonb)
            WHERE run_id=%s::uuid""",
            (Json(diagnostics), run_id),
        )


def load_run(run_id, offset=0, limit=100):
    with PgClient(read_only=True) as pg:
        if not pg.fetch_one("SELECT to_regclass('stock.backtest_run') AS name")["name"]:
            return None
        row = pg.fetch_one(
            "SELECT payload FROM stock.backtest_run WHERE run_id=%s", (run_id,)
        )
        if row is None:
            return None
        trades = pg.fetch_all(
            "SELECT payload FROM stock.backtest_trade WHERE run_id=%s ORDER BY trade_no LIMIT %s OFFSET %s",
            (run_id, limit, offset),
        )
    return json.loads(
        json.dumps(
            {
                **row["payload"],
                "trades": [r["payload"] for r in trades],
                "offset": offset,
                "limit": limit,
            },
            default=str,
        )
    )
