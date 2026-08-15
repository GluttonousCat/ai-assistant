#!/usr/bin/env python3
"""
账号数据库模块
合并 accounts_manager.py 和 accounts_sql_manager.py
"""
import sqlite3
import time
import threading
from typing import Dict, Any, Optional, List

from storage.sqlite.base import BaseDatabase


class AccountDatabase(BaseDatabase):
    """账号数据库"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str = "accounts.db"):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: str = "accounts.db"):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        super().__init__(db_path)

    def _init_tables(self):
        """初始化表结构"""
        # 账号表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                cookie TEXT NOT NULL,
                created_at TEXT,
                is_default BOOLEAN DEFAULT FALSE
            )
        ''')

        # 群组-账号映射表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_account_map (
                group_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL
            )
        ''')

        self.conn.commit()

    def add_account(self, cookie: str, name: str = None) -> Dict[str, Any]:
        """添加账号"""
        if not cookie or not cookie.strip():
            raise ValueError("cookie不能为空")

        account_id = f"acc_{int(time.time() * 1000)}"
        name = name or f"账号{self._get_count() + 1}"

        # 如果是第一个账号，设为默认
        is_default = self._get_count() == 0

        self.cursor.execute('''
            INSERT INTO accounts (id, name, cookie, created_at, is_default)
            VALUES (?, ?, ?, ?, ?)
        ''', (account_id, name, cookie.strip(), self._now_iso(), is_default))

        self.conn.commit()
        return {'id': account_id, 'name': name, 'is_default': is_default}

    def get_account(self, account_id: str, mask_cookie: bool = True) -> Optional[Dict]:
        """获取账号"""
        self.cursor.execute('SELECT * FROM accounts WHERE id = ?', (account_id,))
        row = self.cursor.fetchone()
        if not row:
            return None

        return self._row_to_dict(row, mask_cookie)

    def get_default_account(self, mask_cookie: bool = True) -> Optional[Dict]:
        """获取默认账号"""
        self.cursor.execute('SELECT * FROM accounts WHERE is_default = TRUE')
        row = self.cursor.fetchone()
        if row:
            return self._row_to_dict(row, mask_cookie)

        # 没有默认账号，返回第一个
        self.cursor.execute('SELECT * FROM accounts LIMIT 1')
        row = self.cursor.fetchone()
        return self._row_to_dict(row, mask_cookie) if row else None

    def get_account_for_group(self, group_id: str, mask_cookie: bool = True) -> Optional[Dict]:
        """获取群组对应的账号"""
        self.cursor.execute(
            'SELECT account_id FROM group_account_map WHERE group_id = ?',
            (str(group_id),)
        )
        row = self.cursor.fetchone()

        if row:
            return self.get_account(row[0], mask_cookie)

        return self.get_default_account(mask_cookie)

    def assign_group_account(self, group_id: str, account_id: str) -> bool:
        """分配群组到账号"""
        self.cursor.execute('SELECT 1 FROM accounts WHERE id = ?', (account_id,))
        if not self.cursor.fetchone():
            return False

        self.cursor.execute('''
            INSERT OR REPLACE INTO group_account_map (group_id, account_id)
            VALUES (?, ?)
        ''', (str(group_id), account_id))

        self.conn.commit()
        return True

    def list_accounts(self, mask_cookie: bool = True) -> List[Dict]:
        """列出所有账号"""
        self.cursor.execute('SELECT * FROM accounts ORDER BY created_at')
        return [self._row_to_dict(row, mask_cookie) for row in self.cursor.fetchall()]

    def delete_account(self, account_id: str) -> bool:
        """删除账号"""
        self.cursor.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
        self.cursor.execute('DELETE FROM group_account_map WHERE account_id = ?', (account_id,))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def set_default(self, account_id: str) -> bool:
        """设置默认账号"""
        self.cursor.execute('UPDATE accounts SET is_default = FALSE')
        self.cursor.execute(
            'UPDATE accounts SET is_default = TRUE WHERE id = ?',
            (account_id,)
        )
        self.conn.commit()
        return self.cursor.rowcount > 0

    def _get_count(self) -> int:
        """获取账号数量"""
        self.cursor.execute('SELECT COUNT(*) FROM accounts')
        return self.cursor.fetchone()[0]

    def _now_iso(self) -> str:
        """当前ISO时间"""
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

    def _mask_cookie(self, cookie: str) -> str:
        """掩码显示Cookie"""
        if not cookie or len(cookie) < 8:
            return ""
        return f"***{cookie[-8:]}"

    def _row_to_dict(self, row: tuple, mask_cookie: bool) -> Dict[str, Any]:
        """行转字典"""
        cookie = row[2]
        return {
            'id': row[0],
            'name': row[1],
            'cookie': self._mask_cookie(cookie) if mask_cookie else cookie,
            'created_at': row[3],
            'is_default': bool(row[4])
        }
