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

    def get_stats(self) -> Dict[str, int]:
        """获取统计"""
        stats = {}
        for table in ['topics', 'users', 'comments', 'images']:
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

    def commit(self):
        """提交事务"""
        self.conn.commit()
