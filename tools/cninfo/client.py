# -*- encoding: utf-8 -*-
"""
巨潮资讯网 HTTP 客户端 (纯网络层, 无业务状态)

端点 (公开, 无需登录):
- POST /new/information/topSearch/query        证券代码 -> orgId
- POST /new/hisAnnouncement/query              公告检索 (column=szse 沪深通用, 实测)
- GET  static.cninfo.com.cn/<adjunctUrl>       PDF 下载

反检测 (实测站点有限频抖动, ReadTimeout 常见):
- 直连: trust_env=False 绕过系统代理 (国内站, 走代理反而超时)
- 每请求随机间隔 min~max 秒; 失败指数退避重试
- PDF 下载校验 %PDF 魔数 (防错误页存成假 PDF)
"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from core.logger import get_logger

logger = get_logger(__name__)

SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_BASE = "http://static.cninfo.com.cn"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "http://www.cninfo.com.cn/",
    "Accept": "application/json, text/plain, */*",
}


class CninfoClient:
    """巨潮 HTTP 客户端 (线程安全: 每实例独立 Session, 单线程串行使用)"""

    def __init__(self, min_delay: float = 1.0, max_delay: float = 2.0,
                 timeout: int = 25, retries: int = 4):
        self.session = requests.Session()
        self.session.trust_env = False     # 国内直连, 绕系统代理
        self.session.headers.update(_HEADERS)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.retries = retries
        self._org_id_cache: Dict[str, str] = {}   # sec_code -> orgId (免重复打 topSearch)

    # ---------- 基础 ----------

    def _sleep(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _post_json(self, url: str, data: Dict) -> Optional[Dict]:
        """POST -> JSON, 指数退避重试; 全部失败返回 None"""
        for attempt in range(1, self.retries + 1):
            self._sleep()
            try:
                resp = self.session.post(url, data=data, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:  # noqa: BLE001 网络抖动是常态 (504 限流常见)
                logger.warning(f"cninfo POST 失败 ({attempt}/{self.retries}) "
                               f"{type(e).__name__}: {e}")
                time.sleep(min(30, 3 * attempt ** 2))   # 3/12/27/30s, 504 需长退避
        return None

    # ---------- 公开能力 ----------

    def resolve_org_id(self, sec_code: str) -> Optional[str]:
        """6 位证券代码 -> 巨潮 orgId。

        三级策略 (topSearch 端点限流敏感, 实测高频调用 504):
        ① 内存缓存命中直接返回; ② topSearch 查询; ③ 沪市规则推导兜底
        (实测沪市 orgId = 'gssh0' + 6位代码, 如 600519 -> gssh0600519;
        深市 9900xxxx 无规律, 推导不可用)。
        """
        if sec_code in self._org_id_cache:
            return self._org_id_cache[sec_code]

        org_id = None
        j = self._post_json(SEARCH_URL,
                            {"keyWord": sec_code, "maxSecNum": 5})
        items = j or []
        for it in items:
            if it.get("code") == sec_code:
                org_id = it.get("orgId")
                break
        if org_id is None and items:
            org_id = items[0].get("orgId")
        if org_id is None and sec_code.startswith("6"):
            org_id = f"gssh0{sec_code}"
            logger.info(f"topSearch 未返回, 沪市规则推导 orgId: {org_id}")
        if org_id:
            self._org_id_cache[sec_code] = org_id
        return org_id

    def query_announcements(self, sec_code: str, org_id: str,
                            category: str = "category_ndbg_szsh",
                            se_date: str = "2025-01-01~2025-12-31",
                            page_size: int = 30) -> List[Dict]:
        """公告检索 (翻页至尽)。返回巨潮原始 announcement 列表。

        category: category_ndbg_szsh 年报 / bndbg 半年报 / yjdbg 一季报 /
                  sjdbg 三季报; 缺省年报
        se_date:  'YYYY-MM-DD~YYYY-MM-DD' 披露日期窗口
                  (注意: N 年度报告在 N+1 年披露, 查 2024 年报窗口应跨到 2025)
        """
        out: List[Dict] = []
        page = 1
        while True:
            j = self._post_json(QUERY_URL, {
                "pageNum": page, "pageSize": page_size,
                "column": "szse", "tabName": "fulltext",
                "stock": f"{sec_code},{org_id}",
                "category": category, "seDate": se_date,
                "isHLtitle": "true", "sortName": "", "sortType": "",
                "searchkey": "", "secid": "", "plate": "", "trade": "",
            })
            if not j:
                break
            anns = j.get("announcements") or []
            out.extend(anns)
            total_pages = j.get("totalpages") or 1
            if page >= total_pages or not anns:
                break
            page += 1
        return out

    def download_pdf(self, adjunct_url: str, dest: Path) -> Tuple[bool, int]:
        """下载公告 PDF 到 dest。返回 (ok, size); 校验 %PDF 魔数。"""
        for attempt in range(1, self.retries + 1):
            self._sleep()
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".part")
                with self.session.get(f"{STATIC_BASE}/{adjunct_url}",
                                      timeout=120, stream=True) as r:
                    r.raise_for_status()
                    size = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            f.write(chunk)
                            size += len(chunk)
                with open(tmp, "rb") as f:
                    if f.read(4) != b"%PDF":
                        logger.warning(f"cninfo 下载非 PDF 内容: {adjunct_url}")
                        tmp.unlink(missing_ok=True)
                        return False, 0
                tmp.rename(dest)
                return True, size
            except Exception as e:  # noqa: BLE001
                logger.warning(f"cninfo PDF 下载失败 ({attempt}/{self.retries}) "
                               f"{type(e).__name__}: {e}")
                time.sleep(2 * attempt)
        return False, 0
