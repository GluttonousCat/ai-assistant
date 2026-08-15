#!/usr/bin/env python3
"""
专栏数据库模块
简化版，保留核心功能
"""
from storage.sqlite.base import BaseDatabase


class ColumnsDatabase(BaseDatabase):
    """专栏数据库"""

    def _init_tables(self):
        """初始化表结构"""
        # 专栏表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS columns (
                column_id INTEGER PRIMARY KEY,
                group_id INTEGER,
                title TEXT,
                description TEXT,
                article_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 专栏文章表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS column_articles (
                article_id INTEGER PRIMARY KEY,
                column_id INTEGER,
                title TEXT,
                content TEXT,
                create_time TEXT,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.conn.commit()

    def get_stats(self) -> dict:
        """获取统计"""
        stats = {'columns': 0, 'articles': 0}
        try:
            self.cursor.execute('SELECT COUNT(*) FROM columns')
            stats['columns'] = self.cursor.fetchone()[0]
            self.cursor.execute('SELECT COUNT(*) FROM column_articles')
            stats['articles'] = self.cursor.fetchone()[0]
        except:
            pass
        return stats
