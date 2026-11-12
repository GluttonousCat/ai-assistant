# -*- encoding: utf-8 -*-

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
BULK_STOCK_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
STATIC_BASE = "http://static.cninfo.com.cn"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "http://www.cninfo.com.cn/",
    "Accept": "application/json, text/plain, */*",
}


def is_pdf_file(path) -> bool:
    """%PDF magic-number check (guards against error pages/HTML saved as fake
    PDFs; verify after writing to disk)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


class CninfoClient:
    def __init__(
            self, min_delay: float = 1.0, max_delay: float = 2.0,
            timeout: int = 25, retries: int = 4
    ):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(_HEADERS)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.retries = retries
        self._org_id_cache: Dict[str, str] = {}
        self._stock_list: Optional[List[Dict]] = None

    # ---------- basics ----------
    def _sleep(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _post_json(self, url: str, data: Dict) -> Optional[Dict]:
        """POST -> JSON with exponential-backoff retries; None if all fail."""
        for attempt in range(1, self.retries + 1):
            self._sleep()
            try:
                resp = self.session.post(url, data=data, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:  # noqa: BLE001 network jitter is routine (504 rate-limiting is common)
                logger.warning(f"cninfo POST 失败 ({attempt}/{self.retries}) "
                               f"{type(e).__name__}: {e}")
                time.sleep(min(30, 3 * attempt ** 2))   # 3/12/27/30s; 504 needs a long backoff
        return None

    # ---------- public API ----------

    def fetch_stock_list(self) -> List[Dict]:
        if getattr(self, "_stock_list", None):
            return self._stock_list
        try:
            self._sleep()
            resp = self.session.get(BULK_STOCK_URL, timeout=30)
            resp.raise_for_status()
            items = resp.json().get("stockList") or []
            self._stock_list = items
            self._org_id_cache.update(
                {it["code"]: it.get("orgId", "") for it in items
                 if it.get("code") and it.get("orgId")})
            logger.info(f"巨潮全市场清单加载: {len(items)} 只 (szse_stock.json)")
            return items
        except Exception as e:  # noqa: BLE001 degrade silently when rate-limited/unreachable
            logger.warning(f"巨潮全市场清单不可达: {type(e).__name__}")
            return []

    def fetch_org_map_bulk(self) -> Dict[str, str]:
        """Whole-market {code: orgId} map (built on fetch_stock_list; kept for
        old callers)."""
        return {it["code"]: it.get("orgId", "") for it in self.fetch_stock_list()
                if it.get("code") and it.get("orgId")}

    def resolve_org_id(self, sec_code: str) -> Optional[str]:
        if sec_code in self._org_id_cache:
            return self._org_id_cache[sec_code]
        self.fetch_org_map_bulk()
        if sec_code in self._org_id_cache:
            return self._org_id_cache[sec_code]

        org_id = None
        j = self._post_json(
            SEARCH_URL, {"keyWord": sec_code, "maxSecNum": 5})
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

    def query_announcements(
            self, sec_code: str, org_id: str,
            category: str = "category_ndbg_szsh",
            se_date: str = "2025-01-01~2025-12-31",
            page_size: int = 30
    ) -> List[Dict]:
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

    MAX_DL_ATTEMPTS = 40   # absolute per-file attempt cap (large files often need 5-10 tries under resume mode)

    def download_pdf(self, adjunct_url: str, dest: Path) -> Tuple[bool, int]:
        tmp = dest.with_suffix(".part")
        strikes = 0
        for attempt in range(1, self.MAX_DL_ATTEMPTS + 1):
            self._sleep()
            before = tmp.stat().st_size if tmp.exists() else 0
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                headers = {"Range": f"bytes={before}-"} if before else None
                with self.session.get(f"{STATIC_BASE}/{adjunct_url}",
                                      timeout=120, stream=True,
                                      headers=headers) as r:
                    r.raise_for_status()
                    resume = bool(before and r.status_code == 206)
                    with open(tmp, "ab" if resume else "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            f.write(chunk)
                size = tmp.stat().st_size
                if not is_pdf_file(tmp):
                    logger.warning(f"cninfo 下载非 PDF 内容: {adjunct_url}")
                    tmp.unlink(missing_ok=True)
                    return False, 0
                tmp.rename(dest)
                if attempt > 1:
                    logger.info(f"PDF 续传完成 ({attempt} 次): {dest.name} "
                                f"({size / 1e6:.1f}MB)")
                return True, size
            except Exception as e:  # noqa: BLE001 dropped connections are routine; resume from the bytes already on disk
                grew = (tmp.stat().st_size if tmp.exists() else 0) - before
                logger.warning(f"cninfo PDF 下载中断 ({attempt}/"
                               f"{self.MAX_DL_ATTEMPTS}) @{before}B+{grew}B "
                               f"{type(e).__name__}: {str(e)[:120]}")
                time.sleep(2 * min(attempt, 5))
                strikes = strikes + 1 if grew == 0 else 0
                if strikes >= self.retries:
                    logger.warning(f"cninfo PDF 连续 {strikes} 次零增量, 放弃: "
                                   f"{adjunct_url}")
                    return False, 0
        return False, 0
