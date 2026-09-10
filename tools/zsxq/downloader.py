#!/usr/bin/env python3
"""
文件下载模块
整合原 zsxq_file_downloader.py，限制每次只下载1个文件
"""
import os
import time
import random
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from storage.sqlite.files import FilesDatabase
from storage.sqlite.topics import TopicsDatabase
from core.paths import PathManager
from core.helpers import clean_cookie, sanitize_filename, is_chinese_translated
from tools.zsxq.anti_detect import AntiDetectManager


class FileDownloader:
    """文件下载器 - 限制每次只下载1个文件"""

    def __init__(self, cookie: str, group_id: str, db_path: str = None,
                 download_dir: str = None, log_callback=None):
        self.cookie = clean_cookie(cookie)
        self.group_id = group_id
        self.log_callback = log_callback
        self.stop_flag = False

        # 数据库
        path_manager = PathManager()
        self.db_path = db_path or path_manager.get_files_db_path(group_id)
        self.db = FilesDatabase(self.db_path)

        # 下载目录
        if download_dir is None:
            group_dir = path_manager.get_group_dir(group_id)
            self.download_dir = os.path.join(group_dir, "downloads")
        else:
            self.download_dir = os.path.join(download_dir, f"group_{group_id}")

        os.makedirs(self.download_dir, exist_ok=True)

        # API配置
        self.base_url = "https://api.zsxq.com"
        self.session = requests.Session()

        # 反检测
        self.anti_detect = AntiDetectManager(self.cookie)

        # 下载间隔配置
        self.min_delay = 60      # 最小间隔60秒
        self.max_delay = 120     # 最大间隔120秒
        self.long_sleep_min = 60   # 长休眠最小60秒
        self.long_sleep_max = 120  # 长休眠最大120秒

        # 统计
        self.download_count = 0

        self.log(f"📁 下载目录: {self.download_dir}")

    def log(self, message: str):
        """日志输出"""
        print(message)
        if self.log_callback:
            self.log_callback(message)

    def set_stop_flag(self):
        """设置停止标志"""
        self.stop_flag = True
        self.log("🛑 收到停止信号")

    def is_stopped(self) -> bool:
        """检查是否停止"""
        return self.stop_flag

    def _sleep(self, duration: float):
        """可中断睡眠"""
        start = time.time()
        while time.time() - start < duration:
            if self.is_stopped():
                return
            time.sleep(0.5)

    # ---------- 话题附件下载 (topic_files, 研报多来自此) ----------
    def download_topic_files(self, max_files: int = 1) -> Dict[str, int]:
        """下载话题附件 (topic_files 表), 强制间隔与单文件限制"""
        path_manager = PathManager()
        topics_db_path = path_manager.get_topics_db_path(self.group_id)
        topics_db = TopicsDatabase(topics_db_path)

        # 下载地址: 优先本地 download_url, 否则从 API 获取
        files = topics_db.get_topic_files(limit=max_files, status="pending")

        # 中文翻译版不下载 (保留英文原版)
        for row in files[:]:
            if is_chinese_translated(row.get('name') or ''):
                topics_db.update_topic_file_status(row['file_id'], 'skipped')
                files.remove(row)
                self.log(f"⏭️ 跳过中文版: {row.get('name')}")
        topics_db.commit()

        stats = {'total': len(files), 'success': 0, 'failed': 0}
        self.log(f"📥 下载话题附件 (本批次 {len(files)} 个)")

        for row in files:
            if self.is_stopped():
                break

            file_id = row['file_id']
            name = row.get('name') or f"file_{file_id}"
            self.log(f"📥 话题附件: {name}")

            file_path = os.path.join(self.download_dir, sanitize_filename(name))
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                topics_db.update_topic_file_status(file_id, 'completed', file_path)
                stats['success'] += 1
                continue

            if self.download_topic_file(file_id, row, file_path):
                topics_db.update_topic_file_status(file_id, 'completed', file_path)
                stats['success'] += 1
            else:
                topics_db.update_topic_file_status(file_id, 'failed')
                stats['failed'] += 1

            topics_db.commit()
            self._long_delay()

        return stats

    def download_topic_file(self, file_id: int, row: Dict, file_path: str) -> bool:
        """下载单个话题附件, 复用小文件下载逻辑"""
        url = row.get('download_url')
        if not url:
            self.log(f"   ❌ 无下载地址: file_id={file_id}")
            return False

        try:
            resp = self.session.get(url, timeout=300, stream=True)
            if resp.status_code != 200:
                self.log(f"   ❌ 下载失败: HTTP {resp.status_code}")
                return False

            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                    if self.is_stopped():
                        break

            self.log(f"   ✅ 下载完成: {os.path.basename(file_path)}")
            self.download_count += 1
            return True
        except Exception as e:
            self.log(f"   ❌ 下载异常: {e}")
            if os.path.exists(file_path):
                os.remove(file_path)
            return False

    def fetch_file_list(self, count: int = 20, index: str = None,
                       sort: str = "by_create_time") -> Optional[Dict]:
        """获取文件列表"""
        url = f"{self.base_url}/v2/groups/{self.group_id}/files"
        params = {"count": str(count), "sort": sort}
        if index:
            params["index"] = index

        self.log(f"🌐 获取文件列表: count={count}, sort={sort}")

        for retry in range(3):
            try:
                headers = self.anti_detect.get_headers()
                resp = self.session.get(url, headers=headers, params=params, timeout=30)

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('succeeded'):
                        return data
                    else:
                        self.log(f"⚠️ API返回失败: {data.get('error')}")
                else:
                    self.log(f"⚠️ HTTP {resp.status_code}")

                time.sleep(2 * (retry + 1))
            except Exception as e:
                self.log(f"⚠️ 请求异常: {e}")
                time.sleep(2 * (retry + 1))

        return None

    def get_download_url(self, file_id: int) -> Optional[str]:
        """获取下载链接"""
        url = f"{self.base_url}/v2/files/{file_id}/download_url"

        for retry in range(3):
            try:
                headers = self.anti_detect.get_headers()
                resp = self.session.get(url, headers=headers, timeout=30)

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('succeeded'):
                        return data.get('resp_data', {}).get('download_url')

                time.sleep(2 * (retry + 1))
            except Exception as e:
                self.log(f"⚠️ 获取下载链接失败: {e}")
                time.sleep(2 * (retry + 1))

        return None

    def download_file(self, file_info: Dict) -> bool:
        """下载单个文件"""
        file_data = file_info.get('file', {})
        file_id = file_data.get('file_id') or file_data.get('id')
        file_name = file_data.get('name', 'unknown')
        file_size = file_data.get('size', 0)

        self.log(f"📥 下载: {file_name} ({file_size/1024:.1f}KB)")

        if self.is_stopped():
            return False

        # 清理文件名
        safe_name = sanitize_filename(file_name)
        if not safe_name:
            safe_name = f"file_{file_id}"

        file_path = os.path.join(self.download_dir, safe_name)

        # 检查已存在
        if os.path.exists(file_path):
            existing_size = os.path.getsize(file_path)
            if existing_size == file_size:
                self.log(f"   ✅ 文件已存在且完整，跳过")
                return True

        # 获取下载链接
        download_url = self.get_download_url(file_id)
        if not download_url:
            self.log(f"   ❌ 无法获取下载链接")
            return False

        try:
            # 流式下载
            resp = self.session.get(download_url, timeout=300, stream=True)
            if resp.status_code != 200:
                self.log(f"   ❌ 下载失败: HTTP {resp.status_code}")
                return False

            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                    if self.is_stopped():
                        break

            # 验证大小
            actual_size = os.path.getsize(file_path)
            if file_size > 0 and actual_size != file_size:
                self.log(f"   ⚠️ 大小不匹配: 预期{file_size}, 实际{actual_size}")

            self.log(f"   ✅ 下载完成: {safe_name}")
            self.download_count += 1

            # 强制长休眠（每次下载后）
            self._long_delay()

            return True

        except Exception as e:
            self.log(f"   ❌ 下载异常: {e}")
            if os.path.exists(file_path):
                os.remove(file_path)
            return False

    def _long_delay(self):
        """强制长休眠"""
        delay = random.uniform(self.long_sleep_min, self.long_sleep_max)
        self.log(f"🛌 长休眠: {delay:.0f}秒")
        self._sleep(delay)
        self.log(f"😴 休眠结束")

    def collect_files(self, max_pages: int = 100) -> Dict[str, int]:
        """收集文件列表到数据库"""
        self.log(f"📊 开始收集文件列表...")
        stats = {'total': 0, 'new': 0}
        current_index = None

        for page in range(1, max_pages + 1):
            if self.is_stopped():
                break

            self.log(f"\n📄 收集第{page}页...")

            data = self.fetch_file_list(count=20, index=current_index)
            if not data:
                break

            files = data.get('resp_data', {}).get('files', [])
            if not files:
                self.log("📭 无更多文件")
                break

            for file_info in files:
                file_data = file_info.get('file', {})
                topic_data = file_info.get('topic', {})

                self.db.add_file(
                    file_id=file_data.get('file_id') or file_data.get('id'),
                    topic_id=topic_data.get('topic_id'),
                    name=file_data.get('name', ''),
                    size=file_data.get('size', 0),
                    file_hash=file_data.get('hash', ''),
                    download_count=file_data.get('download_count', 0),
                    create_time=file_data.get('create_time', '')
                )

            self.db.commit()
            stats['total'] += len(files)

            # 检查是否有新文件
            self.db.cursor.execute("SELECT COUNT(*) FROM files WHERE download_status = 'pending'")
            new_count = self.db.cursor.fetchone()[0]
            stats['new'] = new_count

            self.log(f"   ✅ 本页{len(files)}个，累计待下载{new_count}")

            # 下一页
            current_index = data.get('resp_data', {}).get('index')
            if not current_index:
                break

            time.sleep(random.uniform(2, 5))

        self.log(f"\n🎉 收集完成: 共{stats['total']}个，待下载{stats['new']}")
        return stats

    def download_pending(self, max_files: int = 1) -> Dict[str, int]:
        """下载待处理文件"""
        max_files = max(1, min(max_files, 20))  # 每次上限20个, 防误用

        self.log(f"📥 开始下载文件 (限制{max_files}个)")

        # 获取待下载文件
        files = self.db.get_pending_files(limit=max_files, order_by='create_time DESC')

        # 中文翻译版不下载 (保留英文原版): 标 skipped 终态, 不再进 pending
        for file_row in files[:]:
            if is_chinese_translated(file_row[1]):
                self.db.update_status(file_row[0], 'skipped')
                files.remove(file_row)
                self.log(f"⏭️ 跳过中文版: {file_row[1]}")
        if files:
            self.db.commit()

        if not files:
            self.log("📭 无待下载文件")
            return {'total': 0, 'success': 0, 'failed': 0}

        stats = {'total': len(files), 'success': 0, 'failed': 0}

        for file_row in files:
            if self.is_stopped():
                break

            file_id, name, size, download_count, create_time = file_row

            file_info = {
                'file': {
                    'id': file_id,
                    'name': name,
                    'size': size,
                    'download_count': download_count
                }
            }

            result = self.download_file(file_info)

            if result:
                stats['success'] += 1
                safe_name = sanitize_filename(name) or f"file_{file_id}"
                local_path = os.path.join(self.download_dir, safe_name)
                self.db.update_status(file_id, 'completed', local_path)
            else:
                stats['failed'] += 1
                self.db.update_status(file_id, 'failed')

            self.db.commit()

        self.log(f"\n🏁 下载完成: 成功{stats['success']}, 失败{stats['failed']}")
        return stats

    def close(self):
        """关闭资源"""
        self.db.close()
