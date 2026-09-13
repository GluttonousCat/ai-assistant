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
BULK_STOCK_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"   # 全市场 code->orgId 一次拿全
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

    def fetch_org_map_bulk(self) -> Dict[str, str]:
        """全市场 orgId 一次拿全 (szse_stock.json, 巨潮前端搜索下拉的数据源)。

        进程内只拉一次, 结果并入内存缓存; 站点不可达返回空 dict (调用方走降级)。
        """
        if getattr(self, "_bulk_loaded", False):
            return {}
        self._bulk_loaded = True
        try:
            self._sleep()
            resp = self.session.get(BULK_STOCK_URL, timeout=30)
            resp.raise_for_status()
            items = resp.json().get("stockList") or []
            got = {it["code"]: it.get("orgId", "") for it in items
                   if it.get("code") and it.get("orgId")}
            self._org_id_cache.update(got)
            logger.info(f"orgId 批量源加载: {len(got)} 只 (szse_stock.json)")
            return got
        except Exception as e:  # noqa: BLE001 限流/不可达时静默降级
            logger.warning(f"orgId 批量源不可达: {type(e).__name__}")
            return {}

    def resolve_org_id(self, sec_code: str) -> Optional[str]:
        """6 位证券代码 -> 巨潮 orgId。

        四级策略 (topSearch 端点限流最敏感, 实测高频调用 504):
        ① 内存缓存; ② 全量批量源 szse_stock.json (一次请求, 进程内缓存);
        ③ topSearch 单只查询; ④ 沪市规则推导 (orgId = 'gssh0'+6位代码,
        如 600519 -> gssh0600519; 深市 9900xxxx 无规律, 推导不可用)。
        """
        if sec_code in self._org_id_cache:
            return self._org_id_cache[sec_code]
        self.fetch_org_map_bulk()
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

    MAX_DL_ATTEMPTS = 40   # 单文件绝对尝试上限 (续传模式下大文件常需 5-10 次)

    def download_pdf(self, adjunct_url: str, dest: Path) -> Tuple[bool, int]:
        """下载公告 PDF 到 dest。返回 (ok, size); 校验 %PDF 魔数。

        大文件 (年报 30MB+) 实测常被服务端中途掐断, 每连接仅得 3-5MB, 故:
        - 断点续传: 重试带 Range: bytes=N- 从 .part 尾部续传累加;
          服务端不支持 Range (回 200 而非 206) 则整文件重下
        - 有增量即前进: 断流但拿到了字节就不消耗重试名额,
          连续 self.retries 次"零增量"才放弃
        """
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
                from tools.pdf import is_pdf_file
                if not is_pdf_file(str(tmp)):
                    logger.warning(f"cninfo 下载非 PDF 内容: {adjunct_url}")
                    tmp.unlink(missing_ok=True)
                    return False, 0
                tmp.rename(dest)
                if attempt > 1:
                    logger.info(f"PDF 续传完成 ({attempt} 次): {dest.name} "
                                f"({size / 1e6:.1f}MB)")
                return True, size
            except Exception as e:  # noqa: BLE001 断流是常态, 落盘增量后继续续传
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
