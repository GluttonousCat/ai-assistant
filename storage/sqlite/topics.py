#!/usr/bin/env python3
"""
话题数据库模块
整合原 zsxq_database.py 核心功能
"""
import sqlite3
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta

from storage.sqlite.base import BaseDatabase


class TopicsDatabase(BaseDatabase):
    """话题数据库"""

    def _init_tables(self):
        """初始化表结构"""
        # 群组表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT,
                background_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 用户表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                alias TEXT,
                avatar_url TEXT,
                location TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 话题表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS topics (
                topic_id INTEGER PRIMARY KEY,
                group_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                title TEXT,
                create_time TEXT,
                digested BOOLEAN DEFAULT FALSE,
                sticky BOOLEAN DEFAULT FALSE,
                likes_count INTEGER DEFAULT 0,
                comments_count INTEGER DEFAULT 0,
                reading_count INTEGER DEFAULT 0,
                text TEXT,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 评论表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS comments (
                comment_id INTEGER PRIMARY KEY,
                topic_id INTEGER,
                owner_user_id INTEGER,
                parent_comment_id INTEGER,
                text TEXT,
                create_time TEXT,
                likes_count INTEGER DEFAULT 0,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 图片表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS images (
                image_id INTEGER PRIMARY KEY,
                topic_id INTEGER,
                comment_id INTEGER,
                thumbnail_url TEXT,
                large_url TEXT,
                original_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 话题内文件附件表 (知识星球帖子可直接附带文件, 多为研报/PDF)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS topic_files (
                file_id INTEGER PRIMARY KEY,
                topic_id INTEGER,
                name TEXT,
                size INTEGER,
                file_type TEXT,
                download_url TEXT,
                create_time TEXT,
                download_status TEXT DEFAULT 'pending',
                local_path TEXT,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.conn.commit()

    def topic_exists(self, topic_id: int) -> bool:
        """检查话题是否存在"""
        self.cursor.execute('SELECT 1 FROM topics WHERE topic_id = ?', (topic_id,))
        return self.cursor.fetchone() is not None

    def import_topic(self, topic_data: Dict[str, Any]):
        """导入话题"""
        topic_id = topic_data.get('topic_id')
        if not topic_id:
            return

        # 导入群组
        group = topic_data.get('group', {})
        if group:
            self._upsert_group(group)

        # 导入用户
        talk = topic_data.get('talk', {})
        if talk and 'owner' in talk:
            self._upsert_user(talk['owner'])

        # 导入话题
        self._upsert_topic(topic_data)

        # 导入话题内文件附件
        if talk:
            self.import_topic_files(topic_id, talk.get('files', []))

        # 导入评论
        comments = topic_data.get('show_comments', [])
        if comments:
            self.import_comments(topic_id, comments)

    def _upsert_group(self, group: Dict):
        """插入或更新群组"""
        self.cursor.execute('''
            INSERT OR REPLACE INTO groups (group_id, name, type, background_url)
            VALUES (?, ?, ?, ?)
        ''', (
            group.get('group_id'),
            group.get('name', ''),
            group.get('type', ''),
            group.get('background_url', '')
        ))

    def _upsert_user(self, user: Dict):
        """插入或更新用户"""
        self.cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, name, alias, avatar_url, location, description)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            user.get('user_id'),
            user.get('name', ''),
            user.get('alias', ''),
            user.get('avatar_url', ''),
            user.get('location', ''),
            user.get('description', '')
        ))

    def _upsert_topic(self, topic: Dict):
        """插入或更新话题"""
        talk = topic.get('talk', {})
        self.cursor.execute('''
            INSERT OR REPLACE INTO topics
            (topic_id, group_id, type, title, create_time, digested, sticky,
             likes_count, comments_count, reading_count, text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            topic.get('topic_id'),
            topic.get('group', {}).get('group_id', ''),
            topic.get('type', ''),
            topic.get('title', ''),
            topic.get('create_time', ''),
            topic.get('digested', False),
            topic.get('sticky', False),
            topic.get('likes_count', 0),
            topic.get('comments_count', 0),
            topic.get('reading_count', 0),
            talk.get('text', '') if talk else ''
        ))

    def import_comments(self, topic_id: int, comments: List[Dict]):
        """导入评论"""
        for comment in comments:
            comment_id = comment.get('comment_id')
            if not comment_id:
                continue

            owner = comment.get('owner', {})
            if owner:
                self._upsert_user(owner)

            self.cursor.execute('''
                INSERT OR REPLACE INTO comments
                (comment_id, topic_id, owner_user_id, parent_comment_id, text, create_time, likes_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                comment_id,
                topic_id,
                owner.get('user_id') if owner else None,
                comment.get('parent_comment_id'),
                comment.get('text', ''),
                comment.get('create_time', ''),
                comment.get('likes_count', 0)
            ))

    def import_topic_files(self, topic_id: int, files: List[Dict]):
        """导入话题内文件附件 (研报/PDF 等)"""
        for f in files:
            file_id = f.get('file_id') or f.get('id')
            if not file_id:
                continue

            url = ''
            if f.get('download_url'):
                url = f['download_url']
            elif f.get('url'):
                url = f['url']
            elif f.get('file') and isinstance(f['file'], dict):
                url = f['file'].get('download_url', '') or f['file'].get('url', '')

            self.cursor.execute('''
                INSERT OR IGNORE INTO topic_files
                (file_id, topic_id, name, size, file_type, download_url, create_time, download_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            ''', (
                file_id,
                topic_id,
                f.get('name', ''),
                f.get('size', 0),
                f.get('file_type', '') or f.get('type', ''),
                url,
                f.get('create_time', ''),
            ))

    def get_topic_files(self, topic_id: int = None, status: str = None,
                        limit: int = 100) -> List[Dict]:
        """查询话题附件, 支持按文件类型/下载状态筛选"""
        sql = "SELECT * FROM topic_files WHERE 1=1"
        params = []
        if topic_id is not None:
            sql += " AND topic_id = ?"
            params.append(topic_id)
        if status:
            sql += " AND download_status = ?"
            params.append(status)
        sql += " ORDER BY create_time DESC LIMIT ?"
        params.append(limit)

        self.cursor.execute(sql, params)
        cols = [d[0] for d in self.cursor.description]
        return [dict(zip(cols, row)) for row in self.cursor.fetchall()]

    def update_topic_file_status(self, file_id: int, status: str, local_path: str = None):
        """更新附件下载状态"""
        if local_path:
            self.cursor.execute(
                "UPDATE topic_files SET download_status = ?, local_path = ? WHERE file_id = ?",
                (status, local_path, file_id))
        else:
            self.cursor.execute(
                "UPDATE topic_files SET download_status = ? WHERE file_id = ?",
                (status, file_id))

    def get_report_candidates(self, file_types=('pdf', 'doc', 'docx', 'txt', 'md'),
                              limit: int = 500) -> List[Dict]:
        """获取可作为研报的附件 (按后缀过滤)"""
        like_clause = " AND ("
        like_clause += " OR ".join("lower(name) LIKE ?" for _ in file_types)
        like_clause += ")"

        sql = f"""SELECT * FROM topic_files
                  WHERE download_status != 'failed'{like_clause}
                  ORDER BY create_time DESC LIMIT ?"""
        params = [f"%.{t}" for t in file_types] + [limit]

        self.cursor.execute(sql, params)
        cols = [d[0] for d in self.cursor.description]
        return [dict(zip(cols, row)) for row in self.cursor.fetchall()]

    def get_stats(self) -> Dict[str, int]:
        """获取统计"""
        stats = {}
        for table in ['topics', 'users', 'comments', 'images', 'topic_files']:
            try:
                self.cursor.execute(f'SELECT COUNT(*) FROM {table}')
                stats[table] = self.cursor.fetchone()[0]
            except:
                stats[table] = 0
        return stats

    def get_time_range(self) -> Dict[str, Any]:
        """获取时间范围"""
        self.cursor.execute('SELECT COUNT(*) FROM topics')
        total = self.cursor.fetchone()[0]

        if total == 0:
            return {'has_data': False, 'total': 0}

        self.cursor.execute('SELECT MIN(create_time), MAX(create_time) FROM topics')
        result = self.cursor.fetchone()

        return {
            'has_data': True,
            'total': total,
            'oldest': result[0],
            'newest': result[1]
        }

    def get_topic(self, topic_id: int) -> Optional[Dict]:
        """按 ID 获取话题 (含全文)"""
        self.cursor.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.cursor.description]
        return dict(zip(cols, row))

    def fetch_topics_after(self, min_topic_id: int = 0, limit: int = None) -> List[Dict]:
        """按主键递增拉取话题, 用于同步全量"""
        sql = "SELECT * FROM topics WHERE topic_id > ? ORDER BY topic_id ASC"
        params: list = [min_topic_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        self.cursor.execute(sql, params)
        cols = [d[0] for d in self.cursor.description]
        return [dict(zip(cols, row)) for row in self.cursor.fetchall()]

    def get_topic_files_after(self, min_file_id: int = 0, limit: int = None) -> List[Dict]:
        """按主键递增拉取附件, 用于同步全量"""
        sql = "SELECT * FROM topic_files WHERE file_id > ? ORDER BY file_id ASC"
        params: list = [min_file_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        self.cursor.execute(sql, params)
        cols = [d[0] for d in self.cursor.description]
        return [dict(zip(cols, row)) for row in self.cursor.fetchall()]

    def commit(self):
        """提交事务"""
        self.conn.commit()
