#!/usr/bin/env python3
"""
Account database module
Consolidates accounts_manager.py and accounts_sql_manager.py
"""
import time
import threading
from typing import Dict, Any, Optional, List

from storage.sqlite.base import BaseDatabase


class AccountDatabase(BaseDatabase):
    """Account database"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str = "accounts.db"):
        """Singleton pattern"""
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
        """Initialize the table schema"""
        # Accounts table
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                cookie TEXT NOT NULL,
                created_at TEXT,
                is_default BOOLEAN DEFAULT FALSE
            )
        ''')

        # Group-to-account mapping table
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_account_map (
                group_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL
            )
        ''')

        self.conn.commit()

    def add_account(self, cookie: str, name: str = None) -> Dict[str, Any]:
        """Add an account"""
        if not cookie or not cookie.strip():
            raise ValueError("cookie不能为空")

        account_id = f"acc_{int(time.time() * 1000)}"
        name = name or f"账号{self._get_count() + 1}"

        # The first account becomes the default
        is_default = self._get_count() == 0

        self.cursor.execute('''
            INSERT INTO accounts (id, name, cookie, created_at, is_default)
            VALUES (?, ?, ?, ?, ?)
        ''', (account_id, name, cookie.strip(), self._now_iso(), is_default))

        self.conn.commit()
        return {'id': account_id, 'name': name, 'is_default': is_default}

    def get_account(self, account_id: str, mask_cookie: bool = True) -> Optional[Dict]:
        """Get an account"""
        self.cursor.execute('SELECT * FROM accounts WHERE id = ?', (account_id,))
        row = self.cursor.fetchone()
        if not row:
            return None

        return self._row_to_dict(row, mask_cookie)

    def get_default_account(self, mask_cookie: bool = True) -> Optional[Dict]:
        """Get the default account"""
        self.cursor.execute('SELECT * FROM accounts WHERE is_default = TRUE')
        row = self.cursor.fetchone()
        if row:
            return self._row_to_dict(row, mask_cookie)

        # No default account: fall back to the first one
        self.cursor.execute('SELECT * FROM accounts LIMIT 1')
        row = self.cursor.fetchone()
        return self._row_to_dict(row, mask_cookie) if row else None

    def get_account_for_group(self, group_id: str, mask_cookie: bool = True) -> Optional[Dict]:
        """Get the account bound to a group"""
        self.cursor.execute(
            'SELECT account_id FROM group_account_map WHERE group_id = ?',
            (str(group_id),)
        )
        row = self.cursor.fetchone()

        if row:
            return self.get_account(row[0], mask_cookie)

        return self.get_default_account(mask_cookie)

    def assign_group_account(self, group_id: str, account_id: str) -> bool:
        """Bind a group to an account"""
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
        """List all accounts"""
        self.cursor.execute('SELECT * FROM accounts ORDER BY created_at')
        return [self._row_to_dict(row, mask_cookie) for row in self.cursor.fetchall()]

    def delete_account(self, account_id: str) -> bool:
        """Delete an account"""
        self.cursor.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
        self.cursor.execute('DELETE FROM group_account_map WHERE account_id = ?', (account_id,))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def set_default(self, account_id: str) -> bool:
        """Set the default account"""
        self.cursor.execute('UPDATE accounts SET is_default = FALSE')
        self.cursor.execute(
            'UPDATE accounts SET is_default = TRUE WHERE id = ?',
            (account_id,)
        )
        self.conn.commit()
        return self.cursor.rowcount > 0

    def _get_count(self) -> int:
        """Get the account count"""
        self.cursor.execute('SELECT COUNT(*) FROM accounts')
        return self.cursor.fetchone()[0]

    def _now_iso(self) -> str:
        """Current ISO time"""
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

    def _mask_cookie(self, cookie: str) -> str:
        """Mask a cookie for display"""
        if not cookie or len(cookie) < 8:
            return ""
        return f"***{cookie[-8:]}"

    def _row_to_dict(self, row: tuple, mask_cookie: bool) -> Dict[str, Any]:
        """Row to dict"""
        cookie = row[2]
        return {
            'id': row[0],
            'name': row[1],
            'cookie': self._mask_cookie(cookie) if mask_cookie else cookie,
            'created_at': row[3],
            'is_default': bool(row[4])
        }
