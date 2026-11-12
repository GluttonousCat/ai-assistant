# -*- encoding: utf-8 -*-
"""
Database base class
"""
import sqlite3


class BaseDatabase:
    """Database base class"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._init_tables()

    def _init_tables(self):
        """Initialize the table schema; implemented by subclasses"""
        raise NotImplementedError

    def close(self):
        """Close the database connection"""
        if self.conn:
            self.conn.close()

    def __del__(self):
        """Close the connection on destruction"""
        self.close()

    def get_stats(self) -> dict:
        """Get database statistics"""
        raise NotImplementedError
