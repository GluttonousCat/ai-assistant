#!/usr/bin/env python3
"""
数据库基类
"""
import sqlite3
from typing import Optional


class BaseDatabase:
    """数据库基类"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._init_tables()

    def _init_tables(self):
        """初始化表结构，子类实现"""
        raise NotImplementedError

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()

    def __del__(self):
        """析构时关闭连接"""
        self.close()

    def get_stats(self) -> dict:
        """获取数据库统计信息"""
        raise NotImplementedError
