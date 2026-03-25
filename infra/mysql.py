# -*- encoding: utf-8 -*-
"""
@date: 2026/03/24
@file: mysql.py
@author: GluttonousCat
"""
from __future__ import annotations

import mysql.connector
from mysql.connector import Error

from core.logger import get_logger

logger = get_logger(__name__)

class MysqlConnection:
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
