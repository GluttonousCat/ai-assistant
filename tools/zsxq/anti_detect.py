#!/usr/bin/env python3
"""
反检测机制模块
负责请求头生成、延迟控制、频率限制等
"""
import random
import time
from typing import Dict, Optional


class AntiDetectManager:
    """反检测管理器"""

    # User-Agent池
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ]

    def __init__(self, cookie: str):
        self.cookie = cookie
        self.request_count = 0
        self.min_delay = 2.0
        self.max_delay = 5.0
        self.long_delay_interval = 15
        self.debug_mode = False

    def get_headers(self) -> Dict[str, str]:
        """生成随机请求头"""
        ua = random.choice(self.USER_AGENTS)

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7",
            "Cache-Control": "no-cache",
            "Cookie": self.cookie,
            "Origin": "https://wx.zsxq.com",
            "Pragma": "no-cache",
            "Referer": "https://wx.zsxq.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": ua,
            "X-Request-Id": f"req-{random.randint(100000000000, 999999999999)}",
            "X-Timestamp": str(int(time.time())),
            "X-Version": "2.77.0",
        }

        # 随机添加可选头部
        if random.random() > 0.5:
            headers["X-Requested-With"] = "XMLHttpRequest"

        return headers

    def smart_delay(self, is_historical: bool = False):
        """智能延迟"""
        self.request_count += 1

        if is_historical:
            delay = random.uniform(self.min_delay + 1.0, self.max_delay + 2.0)
        else:
            delay = random.uniform(self.min_delay, self.max_delay)

        if self.debug_mode:
            print(f"   ⏱️ 延迟: {delay:.2f}秒 (请求#{self.request_count})")

        time.sleep(delay)

    def long_delay(self):
        """长休眠"""
        delay = random.uniform(60, 120)  # 60-120秒
        print(f"🛌 长休眠: {delay:.1f}秒")
        time.sleep(delay)
