"""
PostgreSQL 底层封装
功能:
- 连接池管理 (psycopg2.pool)
- 上下文管理器 (事务自动提交/回滚)
- DataFrame 批量写入 (COPY / executemany)
- UPSERT (ON CONFLICT)
- 通用查询 / DDL 执行

【边界约束】本模块只通过 PostgreSQL 网络协议(SQL)读写远程数据库,
不执行任何服务器级操作 (无 SSH / 无远端 Shell / 不修改服务器配置)。
远程主机仅作为数据仓库访问。
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

from core.config import get_config

logger = logging.getLogger(__name__)

# pscopg2 连接默认行为
_CONN_KW = dict(application_name="ai-assistant")


class PgPool:
    """PG 连接池 (单例)"""

    _instance: Optional["PgPool"] = None
    _lock = threading.Lock()
    _pool: Optional[SimpleConnectionPool] = None
    _config: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def init(cls, config: Optional[Dict[str, Any]] = None,
             minconn: int = 1, maxconn: int = 10) -> None:
        """初始化连接池 (可重复调用, 仅首次生效)"""
        cfg = config or get_config().pg_config
        if cls._pool is not None:
            # 连接串变化时重建
            if cls._config == cfg:
                return
            cls.close()

        cls._config = dict(cfg)
        try:
            # search_path: 让无 schema 前缀的表名可解析 (LLM 生成 SQL 常省略 schema)
            conn_options = "-c search_path=stock,fin,public"
            cls._pool = SimpleConnectionPool(
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
    def get_conn(cls):
        assert cls._pool is not None, "PG 连接池未初始化, 请先调用 PgPool.init()"
        try:
            return cls._pool.getconn()
        except Exception as e:
            # 连接耗尽或失效时尝试重建
            logger.warning(f"获取 PG 连接失败, 尝试重建连接池: {e}")
            cls.close()
            cls.init()
            return cls._pool.getconn()

    @classmethod
    def put_conn(cls, conn) -> None:
        if cls._pool is not None and conn is not None:
            cls._pool.putconn(conn)

    @classmethod
    def close(cls) -> None:
        if cls._pool is not None:
            cls._pool.closeall()
            cls._pool = None
            logger.info("PG 连接池已关闭")


class PgClient:
    """PG 单据客户端 (上下文管理器)"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        # 自动初始化连接池
        if PgPool._pool is None:
            PgPool.init(config)
        self.conn = None
        self.cur = None

    def __enter__(self) -> "PgClient":
        self.conn = PgPool.get_conn()
        self.conn.autocommit = False
        self.cur = self.conn.cursor()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is not None:
                self.conn.rollback()
                logger.error(f"PG 执行出错, 已回滚: {exc_val}")
            else:
                self.conn.commit()
        finally:
            try:
                if self.cur:
                    self.cur.close()
            finally:
                PgPool.put_conn(self.conn)

    # ---------- DDL / 通用执行 ----------
    def execute(self, sql: str, params: Optional[Sequence] = None) -> int:
        """执行任意 SQL, 返回影响行数"""
        self.cur.execute(sql, params)
        return self.cur.rowcount

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence]) -> int:
        """批量执行"""
        psycopg2.extras.execute_batch(self.cur, sql, seq_of_params)
        return self.cur.rowcount

    def create_schema(self, schema: str) -> None:
        """创建 schema (不存在则创建)"""
        self.cur.execute(
            f'CREATE SCHEMA IF NOT EXISTS "{schema}"'
        )

    def ensure_table(self, sql: str) -> None:
        """执行建表 DDL (IF NOT EXISTS)"""
        self.cur.execute(sql)

    # ---------- 查询 ----------
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
        """查询返回 DataFrame"""
        self.cur.execute(sql, params)
        cols = [d[0] for d in self.cur.description]
        return pd.DataFrame(self.cur.fetchall(), columns=cols)

    # ---------- 工具方法 ----------
    def table_exists(self, schema: str, table: str) -> bool:
        self.cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        return self.cur.fetchone() is not None

    def table_row_count(self, schema: str, table: str) -> int:
        """表的行数估计/精确值 (精确值较大时用 reltuples 加速)"""
        self.cur.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)",
            (f'"{schema}"."{table}"',),
        )
        row = self.cur.fetchone()
        if not row:
            return 0
        return int(row[0]) if row[0] is not None else 0

    def get_columns(self, schema: str, table: str) -> List[Dict[str, Any]]:
        """获取表的列信息"""
        self.cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
        return [{"name": r[0], "type": r[1]} for r in self.cur.fetchall()]

    def upsert_df(self, df: pd.DataFrame, schema: str, table: str,
                  conflict_keys: Sequence[str]) -> int:
        """
        DataFrame UPSERT (按主键冲突更新)
        df 的列名需与表列名一致
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

        # NaN 转 None (PG 不接受 NaN float)
        df = df.where(pd.notna(df), None)
        rows = [tuple(r) for r in df[columns].itertuples(index=False, name=None)]
        # 将 pandas 类型转为原生类型 (int64 -> int 等)
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
        """DataFrame 批量插入 (冲突即忽略, 用于幂等回填)"""
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
    """将 pandas/numpy 标量转为原生 Python 类型"""
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


# 便捷函数
def get_pg(cfg: Optional[Dict[str, Any]] = None) -> PgClient:
    return PgClient(cfg)