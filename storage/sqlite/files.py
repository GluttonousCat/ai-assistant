#!/usr/bin/env python3
"""
文件数据库模块
整合原 zsxq_file_database.py 核心功能
"""
import sqlite3
from typing import Dict, Any, Optional, List
from datetime import datetime

from storage.sqlite.base import BaseDatabase


class FilesDatabase(BaseDatabase):
    """文件数据库"""

    def _init_tables(self):
        """初始化表结构"""
        # 文件表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS files (
                file_id INTEGER PRIMARY KEY,
                topic_id INTEGER,
                name TEXT,
                size INTEGER,
                hash TEXT,
                download_count INTEGER DEFAULT 0,
                create_time TEXT,
                download_status TEXT DEFAULT 'pending',
                local_path TEXT,
                download_time TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 文件收集日志
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS collection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT,
                end_time TEXT,
                total_files INTEGER DEFAULT 0,
                new_files INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running'
            )
        ''')

        self.conn.commit()

    def add_file(self, file_id: int, topic_id: int, name: str, size: int,
                 file_hash: str = '', download_count: int = 0, create_time: str = ''):
        """添加文件记录"""
        self.cursor.execute('''
            INSERT OR IGNORE INTO files
            (file_id, topic_id, name, size, hash, download_count, create_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (file_id, topic_id, name, size, file_hash, download_count, create_time))

    def get_pending_files(self, limit: int = 1, order_by: str = 'create_time DESC') -> List[tuple]:
        """获取待下载文件 (排除音频等非研报文件)"""
        self.cursor.execute(f'''
            SELECT file_id, name, size, download_count, create_time
            FROM files
            WHERE download_status = 'pending'
              AND lower(name) NOT LIKE '%.mp3'
              AND lower(name) NOT LIKE '%.m4a'
              AND lower(name) NOT LIKE '%.wav'
              AND lower(name) NOT LIKE '%.aac'
              AND lower(name) NOT LIKE '%.flac'
            ORDER BY {order_by}
            LIMIT ?
        ''', (limit,))
        return self.cursor.fetchall()

    def get_retry_files(self, limit: int = 3) -> List[tuple]:
        """获取失败待重试文件 (最近失败优先; 失败次数<=5 防无限重试)"""
        self.cursor.execute('''
            SELECT file_id, name, size, download_count, create_time
            FROM files
            WHERE download_status = 'failed' AND fail_count <= 5
              AND lower(name) NOT LIKE '%.mp3'
            ORDER BY download_time DESC
            LIMIT ?
        ''', (limit,))
        return self.cursor.fetchall()

    def update_status(self, file_id: int, status: str, local_path: str = None):
        """更新下载状态"""
        if status == 'failed':
            self.cursor.execute('''
                UPDATE files SET download_status='failed',
                    fail_count = COALESCE(fail_count, 0) + 1,
                    download_time = CURRENT_TIMESTAMP
                WHERE file_id = ?
            ''', (file_id,))
            self.conn.commit()
            return
        if local_path:
            self.cursor.execute('''
                UPDATE files
                SET download_status = ?, local_path = ?, download_time = CURRENT_TIMESTAMP
                WHERE file_id = ?
            ''', (status, local_path, file_id))
        else:
            self.cursor.execute('''
                UPDATE files
                SET download_status = ?, download_time = CURRENT_TIMESTAMP
                WHERE file_id = ?
            ''', (status, file_id))

    def get_stats(self) -> Dict[str, int]:
        """获取统计"""
        stats = {'files': 0, 'pending': 0, 'completed': 0, 'failed': 0}

        self.cursor.execute('SELECT COUNT(*) FROM files')
        stats['files'] = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM files WHERE download_status = 'pending'")
        stats['pending'] = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM files WHERE download_status = 'completed'")
        stats['completed'] = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM files WHERE download_status = 'failed'")
        stats['failed'] = self.cursor.fetchone()[0]

        return stats

    def commit(self):
        """提交事务"""
        self.conn.commit()

    def get_time_range(self) -> Dict[str, Any]:
        """获取文件时间范围"""
        self.cursor.execute('''
            SELECT MIN(create_time), MAX(create_time), COUNT(*)
            FROM files WHERE create_time IS NOT NULL
        ''')
        result = self.cursor.fetchone()

        if not result or result[2] == 0:
            return {'has_data': False}

        return {
            'has_data': True,
            'oldest': result[0],
            'newest': result[1],
            'total': result[2]
        }

    def get_completed_files(self, limit: int = 500) -> List[Dict]:
        """获取已下载完成文件 (供同步 PG 研报库)"""
        self.cursor.execute('''
            SELECT file_id, topic_id, name, size, hash, local_path, create_time
            FROM files
            WHERE download_status = 'completed' AND local_path IS NOT NULL
            ORDER BY create_time DESC
            LIMIT ?
        ''', (limit,))
        cols = [d[0] for d in self.cursor.description]
        return [dict(zip(cols, row)) for row in self.cursor.fetchall()]

    def get_file_by_id(self, file_id: int) -> Optional[Dict]:
        """按 ID 获取文件"""
        self.cursor.execute("SELECT * FROM files WHERE file_id = ?", (file_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.cursor.description]
        return dict(zip(cols, row))

    def start_collection_log(self) -> int:
        """开始收集日志"""
        self.cursor.execute(
            "INSERT INTO collection_log (start_time) VALUES (?)",
            (datetime.now().isoformat(),)
        )
        self.conn.commit()
        return self.cursor.lastrowid

    def end_collection_log(self, log_id: int, total: int, new_files: int):
        """结束收集日志"""
        self.cursor.execute('''
            UPDATE collection_log
            SET end_time = ?, total_files = ?, new_files = ?, status = 'completed'
            WHERE id = ?
        ''', (datetime.now().isoformat(), total, new_files, log_id))
        self.conn.commit()
