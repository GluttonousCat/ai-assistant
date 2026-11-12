# -*- encoding: utf-8 -*-

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_CONN_KW = dict(
    application_name="ai-assistant",
    # TCP keepalive: on a network black hole (silent packet loss) psycopg2's execute()
    # reads the socket with no timeout and blocks forever (the 2026-10-11 backfill hung
    # 40+ min with no error); 30s probes x 3 failures -> disconnect error within ~60s,
    # retried by upper layers
    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
)


def pg_config() -> Dict[str, Any]:
    """PG connection config dict (reads process env directly; .env is loaded app-side by core.config)"""
    return {
        "host": os.environ.get("PG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("PG_PORT", 5432)),
        "user": os.environ.get("PG_USER", "postgres"),
        "password": os.environ.get("PG_PASSWORD", ""),
        "database": os.environ.get("PG_DATABASE", "data"),
    }


def check_pg_config(cfg: Dict[str, Any]) -> List[str]:
    """Config validation: return a problem list (empty list = pass)"""
    problems: List[str] = []
    for key in ("host", "user", "database"):
        if not str(cfg.get(key) or "").strip():
            problems.append(f"{key} 为空")
    try:
        port = int(cfg.get("port", 0))
        if not 1 <= port <= 65535:
            problems.append(f"port 越界: {port}")
    except (TypeError, ValueError):
        problems.append(f"port 非数字: {cfg.get('port')!r}")
    return problems


class PgPool:
    """PG connection pool (singleton; ThreadedConnectionPool + class lock, safe for multithreading/asyncio.to_thread)"""
    _instance: Optional["PgPool"] = None
    _lock = threading.Lock()
    _pool: Optional[ThreadedConnectionPool] = None
    _config: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def init(cls, config: Optional[Dict[str, Any]] = None,
             minconn: int = 2, maxconn: int = 32) -> None:
        """Initialize the pool (repeat calls allowed, only the first takes effect; concurrency-safe)"""
        cfg = config or pg_config()
        for problem in check_pg_config(cfg):
            logger.warning(f"PG 配置异常: {problem}")
        with cls._lock:
            if cls._pool is not None:
                # Rebuild when the connection config changed
                if cls._config == cfg:
                    return
                cls._close_locked()
            cls._config = dict(cfg)
            try:
                # search_path: resolves table names without schema prefix
                # (LLM-generated SQL often omits the schema)
                # statement_timeout: server-side guard so DDL/long queries holding
                # locks cannot starve the pool
                conn_options = ("-c search_path=stock,fin,public "
                                "-c statement_timeout=180000")
                cls._pool = ThreadedConnectionPool(
                    minconn=minconn,
                    maxconn=maxconn,
                    connect_timeout=10,
                    options=conn_options,
                    **_CONN_KW,
                    **cfg,
                )
                logger.info(
                    f"PG 连接池已初始化: {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}"
                )
            except Exception as e:
                cls._pool = None
                logger.error(f"PG 连接池初始化失败: {e}")
                raise

    @classmethod
    def get_conn(cls, pool_wait_retries: int = 60, pool_wait_sec: float = 0.1):
        """Borrow a connection; on pool exhaustion wait with short backoff (concurrency spikes); only connection-level failures rebuild the pool"""
        assert cls._pool is not None, "PG 连接池未初始化, 请先调用 PgPool.init()"
        import time
        from psycopg2.pool import PoolError
        last: Optional[Exception] = None
        for attempt in range(pool_wait_retries + 1):
            try:
                return cls._pool.getconn()
            except PoolError as e:
                # Pool exhausted != pool broken: wait for a return, never rebuild
                # (a rebuild would avalanche the waiters)
                last = e
                time.sleep(pool_wait_sec)
            except Exception as e:
                # Rebuild only on connection-level failure (serialized under lock to
                # avoid duplicate rebuilds across threads; init has its own lock so it
                # must be called outside)
                logger.warning(f"获取 PG 连接失败, 尝试重建连接池: {e}")
                with cls._lock:
                    cls._close_locked()
                cls.init()
                return cls._pool.getconn()
        raise PoolError(f"连接池持续耗尽 (等待 {pool_wait_retries} 次仍无可用连接): {last}")

    @classmethod
    def put_conn(cls, conn, close: bool = False) -> None:
        if cls._pool is not None and conn is not None:
            cls._pool.putconn(conn, close=close)

    @classmethod
    def close(cls) -> None:
        with cls._lock:
            cls._close_locked()

    @classmethod
    def _close_locked(cls) -> None:
        """Close the pool (caller must hold _lock)"""
        if cls._pool is not None:
            try:
                cls._pool.closeall()
            finally:
                cls._pool = None
            logger.info("PG 连接池已关闭")


class PgClient:
    """PG single-ticket client (context manager)"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        # Auto-initialize the connection pool
        if PgPool._pool is None:
            PgPool.init(config)
        self.conn = None
        self.cur = None

    def __enter__(self) -> "PgClient":
        self.conn = PgPool.get_conn()
        # Connection health check: pooled connections may be dead after a network
        # drop; swap in a fresh one when detected
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            self.conn.rollback()  # clear the transaction state opened by SELECT 1
        except Exception:
            logger.warning("PG 池内连接已失效, 丢弃并换新")
            try:
                self.conn.close()
            except Exception:
                pass
            PgPool.put_conn(self.conn, close=True)  # explicitly discard the bad connection, keep it out of the usable pool
            self.conn = PgPool.get_conn()
        self.conn.autocommit = False
        self.cur = self.conn.cursor()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        conn_broken = False
        try:
            if exc_type is not None:
                # Connection-level errors (network drop/server disconnect) need no
                # rollback; mark the connection as broken
                msg = str(exc_val) if exc_val else ""
                if "server closed" in msg or "connection" in msg.lower():
                    conn_broken = True
                    logger.error(f"PG 连接级错误: {msg[:120]}")
                else:
                    self.conn.rollback()
                    logger.error(f"PG 执行出错, 已回滚: {exc_val}")
            else:
                self.conn.commit()
        except Exception as e:
            conn_broken = True
            logger.warning(f"PG 提交/回滚失败 (连接可能已断): {e}")
        finally:
            try:
                if self.cur:
                    self.cur.close()
            except Exception:
                pass
            if conn_broken:
                # Close broken connections outright; never return them to the pool
                try:
                    self.conn.close()
                except Exception:
                    pass
                PgPool.put_conn(self.conn, close=True)
            else:
                PgPool.put_conn(self.conn)

    # ---------- DDL / generic execution ----------
    def execute(self, sql: str, params: Optional[Sequence] = None) -> int:
        """Execute arbitrary SQL, return the affected row count"""
        self.cur.execute(sql, params)
        return self.cur.rowcount

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence]) -> int:
        """Batch execution"""
        psycopg2.extras.execute_batch(self.cur, sql, seq_of_params)
        return self.cur.rowcount

    def create_schema(self, schema: str) -> None:
        """Create a schema (if not exists)"""
        self.cur.execute(
            f'CREATE SCHEMA IF NOT EXISTS "{schema}"'
        )

    def ensure_table(self, sql: str) -> None:
        """Run table-creation DDL (IF NOT EXISTS)"""
        self.cur.execute(sql)

    # ---------- queries ----------
    def fetch_all(self, sql: str, params: Optional[Sequence] = None,
                  as_dict: bool = True) -> List[Dict[str, Any]]:
        self.cur.execute(sql, params)
        if as_dict:
            cols = [d[0] for d in self.cur.description]
            return [dict(zip(cols, row)) for row in self.cur.fetchall()]
        return self.cur.fetchall()

    def fetch_one(self, sql: str, params: Optional[Sequence] = None,
                  as_dict: bool = True) -> Optional[Dict[str, Any]]:
        rows = self.fetch_all(sql, params, as_dict)
        return rows[0] if rows else None

    def fetch_df(self, sql: str, params: Optional[Sequence] = None) -> pd.DataFrame:
        """Query returning a DataFrame"""
        self.cur.execute(sql, params)
        cols = [d[0] for d in self.cur.description]
        return pd.DataFrame(self.cur.fetchall(), columns=cols)

    # ---------- utilities ----------
    def table_exists(self, schema: str, table: str) -> bool:
        self.cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        return self.cur.fetchone() is not None

    def table_row_count(self, schema: str, table: str) -> int:
        """Estimated/exact table row count (reltuples for speed on large tables)"""
        self.cur.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)",
            (f'"{schema}"."{table}"',),
        )
        row = self.cur.fetchone()
        if not row:
            return 0
        return int(row[0]) if row[0] is not None else 0

    def get_columns(self, schema: str, table: str) -> List[Dict[str, Any]]:
        """Get a table's column info"""
        self.cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
        return [{"name": r[0], "type": r[1]} for r in self.cur.fetchall()]

    def upsert_df(self, df: pd.DataFrame, schema: str, table: str,
                  conflict_keys: Sequence[str]) -> int:
        """
        DataFrame UPSERT (update on primary-key conflict)
        df column names must match the table's column names
        """
        if df is None or df.empty:
            return 0
        df = df.copy()
        columns = list(df.columns)
        columns_quoted = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        conflict_cols = ", ".join(f'"{c}"' for c in conflict_keys)
        update_cols = ", ".join(
            f'"{c}" = EXCLUDED."{c}"' for c in columns if c not in conflict_keys
        )

        # NaN -> None (PG rejects NaN floats)
        df = df.where(pd.notna(df), None)
        rows = [tuple(r) for r in df[columns].itertuples(index=False, name=None)]
        # Convert pandas types to native Python types (int64 -> int etc.)
        rows = [_normalize_row(r) for r in rows]

        if update_cols:
            sql = (
                f'INSERT INTO "{schema}"."{table}" ({columns_quoted}) '
                f"VALUES ({placeholders}) "
                f"ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_cols}"
            )
        else:
            sql = (
                f'INSERT INTO "{schema}"."{table}" ({columns_quoted}) '
                f"VALUES ({placeholders}) ON CONFLICT ({conflict_cols}) DO NOTHING"
            )

        psycopg2.extras.execute_batch(self.cur, sql, rows, page_size=5000)
        return len(rows)

    def insert_df(self, df: pd.DataFrame, schema: str, table: str) -> int:
        """DataFrame batch insert (conflicts ignored; for idempotent backfill)"""
        if df is None or df.empty:
            return 0
        df = df.copy()
        columns = list(df.columns)
        columns_quoted = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))

        df = df.where(pd.notna(df), None)
        rows = [_normalize_row(tuple(r)) for r in df[columns].itertuples(index=False, name=None)]

        sql = (
            f'INSERT INTO "{schema}"."{table}" ({columns_quoted}) '
            f"VALUES ({placeholders})"
        )
        psycopg2.extras.execute_batch(self.cur, sql, rows, page_size=5000)
        return len(rows)


def _normalize_row(row: Tuple) -> Tuple:
    """Convert pandas/numpy scalars to native Python types"""
    out = []
    for v in row:
        if isinstance(v, (pd.Timestamp, pd.Timedelta)):
            v = v.to_pydatetime()
        elif isinstance(v, (pd.Int64Dtype,)):
            v = int(v)
        elif hasattr(v, "item"):
            try:
                v = v.item()
            except (TypeError, ValueError):
                pass
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            v = None
        out.append(v)
    return tuple(out)


# Convenience function
def get_pg(cfg: Optional[Dict[str, Any]] = None) -> PgClient:
    return PgClient(cfg)