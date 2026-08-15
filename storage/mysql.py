# -*- encoding: utf-8 -*-
"""
MySQL 底层封装
- MysqlConnection: 连接池 (兼容旧接口, 需显式 init_pool)
- MysqlClient:     轻量客户端 (兼容旧接口)
- MysqlHelper:     推荐使用的底层工具
  支持: 懒初始化连接池 / 上下文管理 / 单条/批量 SQL /
        查询转 DataFrame / DataFrame 批量写入 (INSERT / UPSERT)

用法:
    from storage.mysql import MysqlHelper

    with MysqlHelper() as db:
        rows = db.fetch_all("SELECT * FROM t WHERE code=%s", ("000001",))
    # 或自动读取配置
    with MysqlHelper.from_config() as db:
        db.insert_df(df, table="daily_data", upsert=True, index_cols=["ts_code","trade_date"])
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import mysql.connector
from mysql.connector import Error

from core.logger import get_logger

logger = get_logger(__name__)


# ======================================================================
# 兼容层 (阶段一遗留)
# ======================================================================
class MysqlConnection:
    """MySQL 连接池 (兼容旧接口, 建议新代码统一用 MysqlHelper)"""

    _pool = None

    @classmethod
    def init_pool(cls, **config):
        if cls._pool is None:
            try:
                config.setdefault('pool_size', 5)
                config.setdefault('pool_name', 'mypool')
                cls._pool = mysql.connector.pooling.MySQLConnectionPool(
                    **config)
                logger.info(f"MySQL 连接池 '{config['pool_name']}' "
                            f"初始化成功 (Size: {config['pool_size']})")
            except Error as e:
                logger.error(f"无法创建连接池: {e}")
                raise e

    @classmethod
    def get_connection(cls):
        if cls._pool is None:
            raise Exception("连接池未初始化，请先调用 init_pool()")
        return cls._pool.get_connection()


class MysqlClient:
    """轻量 MySQL 客户端 (兼容旧接口)"""

    def __init__(self):
        self.connection = None
        self.cursor = None

    def __enter__(self):
        self.connection = MysqlConnection.get_connection()
        self.cursor = self.connection.cursor(dictionary=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self.connection.rollback()
                logger.error(f"执行出错，已回滚: {exc_val}")
            else:
                self.connection.commit()
        finally:
            if self.cursor:
                self.cursor.close()
            if self.connection:
                self.connection.close()

    def execute(self, sql, params=None):
        self.cursor.execute(sql, params or ())
        return self.cursor.rowcount

    def fetch_all(self, sql, params=None):
        self.cursor.execute(sql, params or ())
        return self.cursor.fetchall()

    def fetch_one(self, sql, params=None):
        self.cursor.execute(sql, params or ())
        return self.cursor.fetchone()

    def batch_insert(self, sql, data_list):
        self.cursor.executemany(sql, data_list)
        return self.cursor.rowcount


# ======================================================================
# 推荐使用的底层工具
# ======================================================================
class MysqlHelper:
    """
    MySQL 底层工具

    - 连接池懒初始化: 支持传入 dict 或从 core.config 自动读取
    - 上下文管理:    作为 with 块使用时自动提交/回滚并可复用连接
    - DataFrame 支持: to_df / insert_df / upsert_df
    """

    _pool = None
    _pool_config: Dict[str, Any] = {}

    POOL_SIZE = 5
    POOL_NAME = "ai"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        :param config: 连接参数 dict, 例如
            {"host":..., "port":..., "user":..., "password":..., "database":...}
            为 None 时表示使用当前类的共享连接池 (需先 ensure_pool / from_config)
        """
        self._snapshot_host = None
        self._config = config

    # ---------------- 连接池管理 ----------------
    @classmethod
    def _init_pool(cls, config: Dict[str, Any]):
        if cls._pool is None or cls._pool_config != config:
            try:
                cfg = dict(config)
                cfg.setdefault("pool_size", cls.POOL_SIZE)
                cfg.setdefault("pool_name", cls.POOL_NAME)
                cls._pool = mysql.connector.pooling.MySQLConnectionPool(
                    **cfg)
                cls._pool_config = dict(config)
                logger.info(f"MySQL 连接池初始化成功: "
                            f"{config.get('host')}:{config.get('port')}"
                            f"/{config.get('database', '')} "
                            f"(Size: {cfg['pool_size']})")
            except Error as e:
                logger.error(f"无法创建连接池: {e}")
                raise

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]] = None) -> "MysqlHelper":
        """从配置构建 (默认读 .env / config.yaml), 并确保连接池已初始化"""
        if config is None:
            from core.config import get_config
            cfg = get_config()
            config = {
                "host": cfg.env("MYSQL_HOST", "127.0.0.1"),
                "port": int(cfg.env("MYSQL_PORT", 3306)),
                "user": cfg.env("MYSQL_USER", "root"),
                "password": cfg.env("MYSQL_PASSWORD", ""),
                "database": cfg.mysql_database,
            }
        if config.get("database") and "database" not in cls._pool_config:
            pass
        cls._init_pool(config)
        return cls(config=None)

    @classmethod
    def ensure_pool(cls, config: Dict[str, Any]):
        """确保连接池已初始化 (无则初始化)"""
        cls._init_pool(config)

    @classmethod
    def get_connection(cls):
        if cls._pool is None:
            raise RuntimeError("连接池未初始化, 请先调用 MysqlHelper.from_config()")
        return cls._pool.get_connection()

    # ---------------- 连接/游标上下文 ----------------
    @contextmanager
    def _conn_cursor(self) -> Iterator[Tuple[Any, Any]]:
        if self._config:
            # 独立连接 (非池化路径校验)
            self._init_pool(self._config)
        conn = MysqlHelper.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            yield conn, cursor
        finally:
            cursor.close()
            conn.close()

    def __enter__(self):
        """进入上下文: 建立一个连接供本块复用"""
        if self._config:
            self._init_pool(self._config)
        self._ctx_conn = MysqlHelper.get_connection()
        self._ctx_cursor = self._ctx_conn.cursor(dictionary=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self._ctx_conn.rollback()
                logger.error(f"事务已回滚: {exc_val}")
            else:
                self._ctx_conn.commit()
        finally:
            if self._ctx_cursor:
                self._ctx_cursor.close()
            if self._ctx_conn:
                self._ctx_conn.close()

    @property
    def conn(self):
        if not getattr(self, "_ctx_conn", None):
            self.__enter__()
        return self._ctx_conn

    @property
    def cursor(self):
        if not getattr(self, "_ctx_cursor", None):
            self.__enter__()
        return self._ctx_cursor

    # ---------------- 基础 SQL ----------------
    def execute(self, sql: str, params: Optional[Sequence] = None) -> int:
        """执行单条 SQL (写操作), 返回影响行数"""
        self.cursor.execute(sql, params or ())
        self._ctx_conn.commit()
        return self.cursor.rowcount

    def fetch_all(self, sql: str, params: Optional[Sequence] = None) -> List[Dict]:
        self.cursor.execute(sql, params or ())
        return self.cursor.fetchall()

    def fetch_one(self, sql: str, params: Optional[Sequence] = None) -> Optional[Dict]:
        self.cursor.execute(sql, params or ())
        return self.cursor.fetchone()

    def fetch_scalar(self, sql: str, params: Optional[Sequence] = None):
        """取单行单列值"""
        row = self.fetch_one(sql, params)
        return list(row.values())[0] if row else None

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> int:
        """批量执行同一条 SQL"""
        self.cursor.executemany(sql, list(rows))
        self._ctx_conn.commit()
        return self.cursor.rowcount

    # ---------------- 表操作 ----------------
    def table_exists(self, table: str) -> bool:
        db_name = self.fetch_scalar("SELECT DATABASE()")
        row = self.fetch_one(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (db_name, table),
        )
        return row is not None

    def create_table(self, table: str, columns_sql: str,
                     if_not_exists: bool = True) -> None:
        """建表
        :param columns_sql: 列定义部分, 如 "id INT PRIMARY KEY, name VARCHAR(32)"
        """
        not_exists = "IF NOT EXISTS " if if_not_exists else ""
        sql = f"CREATE TABLE {not_exists}`{table}` ({columns_sql})"
        self.execute(sql)
        logger.info(f"表 {table} 已创建/存在")

    # ---------------- DataFrame / 批量写入 ----------------
    def insert_df(self, df, table: str,
                  batch_size: int = 1000,
                  replace: bool = True) -> int:
        """
        将 DataFrame 批量 INSERT 到表中

        :param df: pandas DataFrame
        :param table: 目标表名
        :param batch_size: 每批行数
        :param replace: True 用 INSERT INTO ... ON DUPLICATE KEY UPDATE (UPSERT)
                        False 用普通 INSERT
        :return: 影响行数
        """
        import pandas as pd
        if df is None or len(df) == 0:
            return 0

        cols = list(df.columns)

        # 处理 NaN/NaT -> None (MySQL 需要 NULL)
        df = df.where(pd.notnull(df), None)

        cols_str = ", ".join(f"`{c}`" for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))

        if replace:
            update_part = ", ".join(
                f"`{c}`=VALUES(`{c}`)" for c in cols)
            sql = (
                f"INSERT INTO `{table}` ({cols_str}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_part}"
            )
        else:
            sql = f"INSERT INTO `{table}` ({cols_str}) VALUES ({placeholders})"

        total = 0
        for start in range(0, len(df), batch_size):
            chunk = df.iloc[start: start + batch_size]
            rows = [tuple(row) for row in chunk.to_numpy()]
            self.cursor.executemany(sql, rows)
            self._ctx_conn.commit()
            total += self.cursor.rowcount
        return total

    def to_df(self, sql: str, params: Optional[Sequence] = None):
        """查询结果转 DataFrame"""
        rows = self.fetch_all(sql, params)
        if not rows:
            return None
        import pandas as pd
        return pd.DataFrame(rows)


# 便捷函数
def get_helper() -> MysqlHelper:
    """便捷: 返回一个基于配置的 MysqlHelper (连接池已初始化)"""
    return MysqlHelper.from_config()