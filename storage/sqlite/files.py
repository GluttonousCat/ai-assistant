#!/usr/bin/env python3
"""
File database module
Consolidates the core functionality of the former zsxq_file_database.py
"""
from typing import Dict, Any, Optional, List
from datetime import datetime

from storage.sqlite.base import BaseDatabase


class FilesDatabase(BaseDatabase):
    """File database"""

    def _init_tables(self):
        """Initialize the table schema"""
        # Files table
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

        # File collection log
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
        """Add a file record"""
        self.cursor.execute('''
            INSERT OR IGNORE INTO files
            (file_id, topic_id, name, size, hash, download_count, create_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (file_id, topic_id, name, size, file_hash, download_count, create_time))

    def get_pending_files(self, limit: int = 1, order_by: str = 'create_time DESC') -> List[tuple]:
        """Get files pending download (excludes audio and other non-report files)"""
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
        """Get failed files to retry (most recent failure first; fail_count<=5 prevents infinite retries)"""
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
        """Update the download status"""
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
        """Get statistics"""
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
        """Commit the transaction"""
        self.conn.commit()

    def get_time_range(self) -> Dict[str, Any]:
        """Get the file time range"""
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
        """Get downloaded/completed files (for syncing into the PG report store)"""
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
        """Get a file by ID"""
        self.cursor.execute("SELECT * FROM files WHERE file_id = ?", (file_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.cursor.description]
        return dict(zip(cols, row))

    def start_collection_log(self) -> int:
        """Start a collection log"""
        self.cursor.execute(
            "INSERT INTO collection_log (start_time) VALUES (?)",
            (datetime.now().isoformat(),)
        )
        self.conn.commit()
        return self.cursor.lastrowid

    def end_collection_log(self, log_id: int, total: int, new_files: int):
        """Finish a collection log"""
        self.cursor.execute('''
            UPDATE collection_log
            SET end_time = ?, total_files = ?, new_files = ?, status = 'completed'
            WHERE id = ?
        ''', (datetime.now().isoformat(), total, new_files, log_id))
        self.conn.commit()
