# -*- encoding: utf-8 -*-
"""
@file: crawler.py
@date: 2026/04/10
@author: GluttonousCat
"""
import logging

import requests
import time
import random
from typing import Dict, Optional
from datetime import datetime, timedelta

from storage.sqlite.topics import TopicsDatabase
from core.paths import PathManager
from core.helpers import clean_cookie, decrement_time
from tools.zsxq.anti_detect import AntiDetectManager


class ZSXQCrawler:
    def __init__(self, cookie: str, group_id: str, db_path: str = None, log_callback=None):
        self.cookie = clean_cookie(cookie)
        self.group_id = group_id
        self.log_callback = log_callback
        self.stop_flag = False
        self.stop_check_func = None

        path_manager = PathManager()
        if db_path is None:
            db_path = path_manager.get_topics_db_path(group_id)
        self.db = TopicsDatabase(db_path)
        self.db_path = db_path

        self.session = requests.Session()

        self.base_url = "https://api.zsxq.com"
        self.api_endpoint = f"/v2/groups/{group_id}/topics"

        self.anti_detect = AntiDetectManager(self.cookie)

        self.request_count = 0
        self.page_count = 0
        self.timestamp_offset_ms = 1

        self.use_custom_intervals = False
        self.custom_settings = {}

        self.log(f"🚀 爬虫初始化完成 | 群组: {group_id}")
        self._show_db_status()

    def log(self, message: str):
        print(message)
        if self.log_callback:
            self.log_callback(message)

    def set_stop_flag(self):
        self.stop_flag = True
        self.log("🛑 收到停止信号")

    def is_stopped(self) -> bool:
        if self.stop_flag:
            return True
        if self.stop_check_func and self.stop_check_func():
            self.stop_flag = True
            return True
        return False

    def _interruptible_sleep(self, duration: float):
        start = time.time()
        while time.time() - start < duration:
            if self.is_stopped():
                return
            time.sleep(0.1)

    def set_custom_intervals(self, **kwargs):
        self.use_custom_intervals = any(kwargs.values())
        self.custom_settings = kwargs
        if self.use_custom_intervals:
            self.log("🔧 使用自定义间隔设置")

    def _show_db_status(self):
        stats = self.db.get_stats()
        self.log(f"📊 数据库: 话题{stats.get('topics', 0)}, 用户{stats.get('users', 0)}, 评论{stats.get('comments', 0)}")

    def _get_headers(self) -> Dict[str, str]:
        return self.anti_detect.get_headers()

    def _smart_delay(self, is_historical: bool = False):
        if self.use_custom_intervals:
            min_d = self.custom_settings.get('crawl_interval_min', 2.0)
            max_d = self.custom_settings.get('crawl_interval_max', 5.0)
            delay = random.uniform(min_d, max_d)
        else:
            delay = random.uniform(2.0, 5.0) if not is_historical else random.uniform(3.0, 7.0)

        self._interruptible_sleep(delay)

    def fetch_topics(self, scope: str = "all", count: int = 20,
                    end_time: Optional[str] = None, is_historical: bool = False) -> Optional[Dict]:
        self._smart_delay(is_historical)

        url = f"{self.base_url}{self.api_endpoint}"
        headers = self._get_headers()
        params = {"scope": scope, "count": str(count)}
        if end_time:
            params["end_time"] = end_time

        self.log(f"🌐 请求: scope={scope}, count={count}")

        try:
            resp = self.session.get(url, headers=headers, params=params, timeout=10)
            self.request_count += 1

            if resp.status_code == 200:
                data = resp.json()
                if data.get('succeeded'):
                    topics = data.get('resp_data', {}).get('topics', [])
                    self.log(f"✅ 获取 {len(topics)} 个话题")
                    return data
                else:
                    self.log(f"❌ API错误: {data.get('error')}")
            else:
                self.log(f"❌ HTTP {resp.status_code}")
        except Exception as e:
            self.log(f"❌ 请求异常: {e}")

        return None

    def fetch_comments(self, topic_id: int, begin_time: str = None, count: int = 30) -> Optional[Dict]:
        url = f"{self.base_url}/v2/topics/{topic_id}/comments"
        params = {'sort': 'asc', 'count': count, 'with_sticky': 'true'}
        if begin_time:
            params['begin_time'] = begin_time

        for retry in range(3):
            try:
                headers = self._get_headers()
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('succeeded'):
                        return data
            except Exception as e:
                self.log(f"⚠️ 评论获取失败(重试{retry+1}): {e}")
                time.sleep(2)

        return None

    def store_topics(self, data: Dict) -> Dict[str, int]:
        if not data or not data.get('succeeded'):
            return {'new': 0, 'updated': 0, 'errors': 0}

        topics = data.get('resp_data', {}).get('topics', [])
        stats = {'new': 0, 'updated': 0, 'errors': 0}

        for topic in topics:
            if self.is_stopped():
                break

            try:
                topic_id = topic.get('topic_id')
                exists = self.db.topic_exists(topic_id)
                self.db.import_topic(topic)

                # 获取额外评论
                comments_count = topic.get('comments_count', 0)
                if comments_count > 8:
                    self._fetch_all_comments(topic_id, comments_count)

                stats['updated' if exists else 'new'] += 1
            except Exception as e:
                stats['errors'] += 1
                self.log(f"⚠️ 导入失败: {e}")

        self.db.commit()
        return stats

    def _fetch_all_comments(self, topic_id: int, total_count: int):
        if total_count <= 8:
            return

        self.log(f"📝 获取话题 {topic_id} 的评论...")
        all_comments = []
        begin_time = None

        while len(all_comments) < total_count:
            if self.is_stopped():
                break

            data = self.fetch_comments(topic_id, begin_time)
            if not data:
                break

            comments = data.get('resp_data', {}).get('comments', [])
            if not comments:
                break

            all_comments.extend(comments)

            if len(comments) < 30:
                break

            last_time = comments[-1].get('create_time')
            if last_time:
                begin_time = self._increment_time(last_time)

            time.sleep(1)

        if all_comments:
            self.db.import_comments(topic_id, all_comments)
            self.log(f"✅ 导入 {len(all_comments)} 条评论")

    def _increment_time(self, time_str: str) -> str:
        try:
            dt = datetime.fromisoformat(time_str.replace('+0800', '+08:00'))
            dt = dt + timedelta(milliseconds=1)
            return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0800'
        except Exception as e:
            logging.info(e)
            return time_str

    def crawl_latest(self, count: int = 20) -> Dict[str, int]:
        self.log(f"\n🆕 爬取最新 {count} 个话题...")
        data = self.fetch_topics(scope="all", count=count)
        if data:
            stats = self.store_topics(data)
            self.log(f"💾 新增{stats['new']}, 更新{stats['updated']}")
            return stats
        return {'new': 0, 'updated': 0, 'errors': 1}

    def crawl_historical(self, pages: int = 10, per_page: int = 20) -> Dict[str, int]:
        self.log(f"\n📚 爬取历史: {pages}页 x {per_page}条")
        total = {'new': 0, 'updated': 0, 'errors': 0, 'pages': 0}
        end_time = None

        for page in range(1, pages + 1):
            if self.is_stopped():
                break

            self.log(f"\n📄 页面 {page}/{pages}")

            data = self.fetch_topics(scope="all", count=per_page,
                                    end_time=end_time, is_historical=True)
            if not data:
                total['errors'] += 1
                continue

            topics = data.get('resp_data', {}).get('topics', [])
            if not topics:
                self.log("📭 无更多数据")
                break

            stats = self.store_topics(data)
            total['new'] += stats['new']
            total['updated'] += stats['updated']
            total['errors'] += stats['errors']
            total['pages'] += 1

            # 准备下一页时间戳
            end_time = decrement_time(topics[-1].get('create_time'), self.timestamp_offset_ms)

            # 长休眠检查
            if page % 15 == 0:
                self.anti_detect.long_delay()

        self.log(f"\n🏁 完成: 新增{total['new']}, 更新{total['updated']}, 页数{total['pages']}")
        return total

    def crawl_incremental(self, pages: int = 10, per_page: int = 20) -> Dict[str, int]:
        """增量爬取（从数据库最老时间继续）"""
        time_info = self.db.get_time_range()
        if not time_info['has_data']:
            self.log("❌ 数据库为空，请先进行历史爬取")
            return {'new': 0, 'updated': 0, 'errors': 1}

        oldest = time_info['oldest']
        self.log(f"\n📈 增量爬取 | 当前最老: {oldest}")

        end_time = decrement_time(oldest, self.timestamp_offset_ms)
        return self._crawl_with_end_time(end_time, pages, per_page)

    def crawl_latest_until_complete(self, per_page: int = 20) -> Dict[str, int]:
        time_info = self.db.get_time_range()
        self.log(f"\n🔄 智能更新 | 现有{time_info['total']}条")

        total = {'new': 0, 'updated': 0, 'errors': 0, 'pages': 0}
        end_time = None

        while True:
            if self.is_stopped():
                break

            data = self.fetch_topics(scope="all", count=per_page, end_time=end_time)
            if not data:
                break

            topics = data.get('resp_data', {}).get('topics', [])
            if not topics:
                break

            # 检查是否全部已存在
            all_exist = all(self.db.topic_exists(t['topic_id']) for t in topics)
            if all_exist:
                self.log("✅ 全部已存在，更新完成")
                break

            stats = self.store_topics(data)
            total['new'] += stats['new']
            total['updated'] += stats['updated']
            total['pages'] += 1

            end_time = decrement_time(topics[-1].get('create_time'), self.timestamp_offset_ms)

        self.log(f"\n🏁 完成: 新增{total['new']}, 更新{total['updated']}")
        return total

    def _crawl_with_end_time(self, end_time: str, pages: int, per_page: int) -> Dict[str, int]:
        """通用爬取逻辑"""
        total = {'new': 0, 'updated': 0, 'errors': 0, 'pages': 0}

        for page in range(1, pages + 1):
            if self.is_stopped():
                break

            self.log(f"\n📄 页面 {page}/{pages}")

            data = self.fetch_topics(scope="all", count=per_page,
                                    end_time=end_time, is_historical=True)
            if not data:
                total['errors'] += 1
                continue

            topics = data.get('resp_data', {}).get('topics', [])
            if not topics:
                self.log("📭 无更多数据")
                break

            stats = self.store_topics(data)
            total['new'] += stats['new']
            total['updated'] += stats['updated']
            total['pages'] += 1

            end_time = decrement_time(topics[-1].get('create_time'), self.timestamp_offset_ms)

        return total

    def close(self):
        self.db.close()
