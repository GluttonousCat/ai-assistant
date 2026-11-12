#!/usr/bin/env python3
"""
File download module.
Consolidated from the former zsxq_file_downloader.py; limited to downloading
one file at a time.
"""
import os
import time
import random
import requests
from typing import Dict, Optional

from storage.sqlite.files import FilesDatabase
from storage.sqlite.topics import TopicsDatabase
from core.paths import PathManager
from core.helpers import clean_cookie, sanitize_filename, is_chinese_translated
from tools.zsxq.anti_detect import AntiDetectManager


class FileDownloader:
    """File downloader - downloads at most one file per batch."""

    def __init__(self, cookie: str, group_id: str, db_path: str = None,
                 download_dir: str = None, log_callback=None):
        self.cookie = clean_cookie(cookie)
        self.group_id = group_id
        self.log_callback = log_callback
        self.stop_flag = False

        # Databases
        path_manager = PathManager()
        self.db_path = db_path or path_manager.get_files_db_path(group_id)
        self.db = FilesDatabase(self.db_path)

        # Download directory
        if download_dir is None:
            group_dir = path_manager.get_group_dir(group_id)
            self.download_dir = os.path.join(group_dir, "downloads")
        else:
            self.download_dir = os.path.join(download_dir, f"group_{group_id}")

        os.makedirs(self.download_dir, exist_ok=True)

        # API config
        self.base_url = "https://api.zsxq.com"
        self.session = requests.Session()

        # Anti-detection
        self.anti_detect = AntiDetectManager(self.cookie)

        # Download pacing config (config.yaml zsxq: section; falls back to
        # built-in defaults)
        from core.config import get_config
        _cfg = get_config()
        self.long_sleep_min = float(_cfg.get("zsxq.download_long_sleep_min", 60))
        self.long_sleep_max = float(_cfg.get("zsxq.download_long_sleep_max", 120))

        # Stats
        self.download_count = 0

        self.log(f"📁 下载目录: {self.download_dir}")

    def log(self, message: str):
        """Log output."""
        print(message)
        if self.log_callback:
            self.log_callback(message)

    def set_stop_flag(self):
        """Set the stop flag."""
        self.stop_flag = True
        self.log("🛑 收到停止信号")

    def is_stopped(self) -> bool:
        """Check whether stopped."""
        return self.stop_flag

    def _sleep(self, duration: float):
        """Interruptible sleep."""
        start = time.time()
        while time.time() - start < duration:
            if self.is_stopped():
                return
            time.sleep(0.5)

    # ---------- Topic-attachment download (topic_files; most research reports come from here) ----------
    def download_topic_files(self, max_files: int = 1) -> Dict[str, int]:
        """Download topic attachments (topic_files table); enforced pacing and per-batch file cap."""
        path_manager = PathManager()
        topics_db_path = path_manager.get_topics_db_path(self.group_id)
        topics_db = TopicsDatabase(topics_db_path)

        # Download URL: prefer the locally stored download_url; otherwise
        # fetch it from the API later
        # Over-fetch so the quota is not eaten by Chinese-translated copies
        # (same rationale as in download_pending)
        files = topics_db.get_topic_files(limit=max_files * 3, status="pending")

        # Skip Chinese-translated copies (keep the English originals)
        skipped = 0
        for row in files[:]:
            if is_chinese_translated(row.get('name') or ''):
                topics_db.update_topic_file_status(row['file_id'], 'skipped')
                files.remove(row)
                skipped += 1
        if skipped:
            self.log(f"⏭️ 跳过中文版: {skipped} 个")
        topics_db.commit()
        files = files[:max_files]

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
        """Download a single topic attachment, reusing the small-file download logic."""
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
        """Fetch the file list."""
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
        """Fetch the download URL."""
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
        """Download a single file."""
        file_data = file_info.get('file', {})
        file_id = file_data.get('file_id') or file_data.get('id')
        file_name = file_data.get('name', 'unknown')
        file_size = file_data.get('size', 0)

        self.log(f"📥 下载: {file_name} ({file_size/1024:.1f}KB)")

        if self.is_stopped():
            return False

        # Sanitize the file name
        safe_name = sanitize_filename(file_name)
        if not safe_name:
            safe_name = f"file_{file_id}"

        file_path = os.path.join(self.download_dir, safe_name)

        # Skip if already downloaded
        if os.path.exists(file_path):
            existing_size = os.path.getsize(file_path)
            if existing_size == file_size:
                self.log(f"   ✅ 文件已存在且完整，跳过")
                return True

        # Fetch the download URL
        download_url = self.get_download_url(file_id)
        if not download_url:
            self.log(f"   ❌ 无法获取下载链接")
            return False

        try:
            # Streamed download
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

            # Verify size
            actual_size = os.path.getsize(file_path)
            if file_size > 0 and actual_size != file_size:
                self.log(f"   ⚠️ 大小不匹配: 预期{file_size}, 实际{actual_size}")

            self.log(f"   ✅ 下载完成: {safe_name}")
            self.download_count += 1

            # Enforced long sleep (after every download)
            self._long_delay()

            return True

        except Exception as e:
            self.log(f"   ❌ 下载异常: {e}")
            if os.path.exists(file_path):
                os.remove(file_path)
            return False

    def _long_delay(self):
        """Enforced long sleep."""
        delay = random.uniform(self.long_sleep_min, self.long_sleep_max)
        self.log(f"🛌 长休眠: {delay:.0f}秒")
        self._sleep(delay)
        self.log(f"😴 休眠结束")

    def collect_files(self, max_pages: int = 100) -> Dict[str, int]:
        """Collect the file list into the database."""
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

            # Count files still pending download
            self.db.cursor.execute("SELECT COUNT(*) FROM files WHERE download_status = 'pending'")
            new_count = self.db.cursor.fetchone()[0]
            stats['new'] = new_count

            self.log(f"   ✅ 本页{len(files)}个，累计待下载{new_count}")

            # Next page
            current_index = data.get('resp_data', {}).get('index')
            if not current_index:
                break

            time.sleep(random.uniform(2, 5))

        self.log(f"\n🎉 收集完成: 共{stats['total']}个，待下载{stats['new']}")
        return stats

    def download_pending(self, max_files: int = 1) -> Dict[str, int]:
        """Download pending files."""
        max_files = max(1, min(max_files, 20))  # hard cap of 20 per run, guard against misuse

        self.log(f"📥 开始下载文件 (限制{max_files}个)")

        # Over-fetch 3x then filter: the batch quota may be eaten by
        # Chinese-translated copies (leaving too few after filtering) —
        # otherwise "limit=1 lands on a translated copy -> empty batch ->
        # the caller's loop misreads 'no pending files' and exits the round"
        files = self.db.get_pending_files(limit=max_files * 3, order_by='create_time DESC')

        # Skip Chinese-translated copies (keep the English originals):
        # mark skipped as a terminal state so they never re-enter pending
        skipped = []
        for file_row in files[:]:
            if is_chinese_translated(file_row[1]):
                self.db.update_status(file_row[0], 'skipped')
                files.remove(file_row)
                skipped.append(file_row[1])
        if skipped:
            self.db.commit()
            for name in skipped[:3]:
                self.log(f"⏭️ 跳过中文版: {name}")
            if len(skipped) > 3:
                self.log(f"⏭️ …共跳过中文版 {len(skipped)} 个")
        files = files[:max_files]

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
        """Release resources."""
        self.db.close()
