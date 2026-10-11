# -*- encoding: utf-8 -*-
"""
巨潮公告下载编排: 代码解析 -> 公告查询 -> 筛选 -> 元数据落盘 -> PDF 下载

幂等 (重跑安全):
- 元数据: announcement_id (巨潮原生) 为键 upsert (output/cninfo/meta/<ts_code>.json)
- 下载:   file_path 已回填且文件在盘且未 --force -> 跳过
          (存在性检查而非水位线 — 下载完成顺序无序)

只认证券代码 (6 位或 ts_code); 股票名称解析是调用方 (agent 词表) 的职责。

用法:
    python -m tools.cninfo.downloader --stock 300308,600519 --years 2023,2024,2025
    python -m tools.cninfo.downloader --stock 300308 --no-download
    python -m tools.cninfo.downloader --stock 300308 --all-periodic   # 含半年报/季报
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import store
from .client import CninfoClient
from .filters import (classify_category, normalize_ts_code, parse_report_year,
                      pick_full_report)

logger = logging.getLogger(__name__)

DOWNLOAD_ROOT = Path("output/cninfo/downloads")

# 查询用类别参数: (类别码, 巨潮 category 参数, 披露窗口年份偏移)
_PERIODIC_CATEGORIES = {
    "ndbg": "category_ndbg_szsh",    # 年报: 次年 1-4 月披露
    "bndbg": "category_bndbg_szsh",  # 半年报: 当年 7-8 月
    "yjdbg": "category_yjdbg_szsh",  # 一季报: 当年 4 月
    "sjdbg": "category_sjdbg_szsh",  # 三季报: 当年 10 月
}

# 元数据条目里保留下载回填的字段 (upsert 公告时不覆盖)
_DL_FIELDS = ("file_path", "file_size", "download_status", "downloaded_at")


class CninfoDownloader:
    def __init__(self, client: Optional[CninfoClient] = None):
        self.client = client or CninfoClient()

    # ---------- 元数据 ----------

    @staticmethod
    def _upsert_announcement(meta: Dict, ann: Dict, ts_code: str) -> None:
        """巨潮原始条目 -> 清洗投影, 以 announcement_id 为键合并 (保留下载回填字段)"""
        title = ann.get("announcementTitle") or ""
        aid = str(ann.get("announcementId") or ann.get("adjunctUrl", ""))
        ts = ann.get("announcementTime")
        entry = {
            "ts_code": ts_code,
            "sec_code": ann.get("secCode"),
            "sec_name": ann.get("secName"),
            "title": title,
            "category": classify_category(title),
            "report_year": parse_report_year(title),
            "announce_date": (datetime.fromtimestamp(ts / 1000).date().isoformat()
                              if ts else None),
            "announce_ts": ts,          # 原始毫秒, 全文挑选的排序键
            "adjunct_url": ann.get("adjunctUrl"),
        }
        entry.update({k: v for k, v in (meta["announcements"].get(aid) or {}).items()
                      if k in _DL_FIELDS})
        meta["announcements"][aid] = entry

    # ---------- orgId ----------

    def _resolve_org_id(self, sec_code: str) -> Optional[str]:
        """orgId 解析: 文件映射 (org_map.json) -> client 四级策略 -> 回写文件。
        每只股票一生只真正解析一次 — topSearch 限流时代重复解析是主要失败源。"""
        org_map = store.load_org_map()
        if sec_code in org_map:
            return org_map[sec_code]
        org_id = self.client.resolve_org_id(sec_code)
        if org_id:
            org_map[sec_code] = org_id
            store.save_org_map(org_map)
        return org_id

    # ---------- 主流程 ----------

    def sync_stock(self, stock: str, years: List[int], download: bool = True,
                   force: bool = False, categories: Tuple[str, ...] = ("ndbg",)
                   ) -> Dict[str, int]:
        """同步一只股票的定期报告: 查询->筛选->落盘->(可选)下载。返回统计。"""
        stats = {"queried": 0, "saved": 0, "downloaded": 0,
                 "skipped": 0, "failed": 0}
        ts_code = normalize_ts_code(stock)
        if not ts_code:
            logger.error(f"未识别证券代码: {stock} (只认 6 位代码或 ts_code, "
                         f"名称解析归调用方)")
            stats["failed"] += 1
            return stats
        sec_code = ts_code.split(".")[0]

        org_id = self._resolve_org_id(sec_code)
        if not org_id:
            logger.error(f"巨潮未找到 orgId: {sec_code} ({stock})")
            stats["failed"] += 1
            return stats
        logger.info(f"{ts_code} (orgId={org_id}), 年份 {years}")
        meta = store.load_stock_meta(ts_code)

        # 每类别一次宽窗查询 (覆盖全部年份, 5年4类=4次请求而非20次;
        # 分号多类别参数实测不可用), 落盘全部, 下载按 (year,cat) 挑全文
        se_date = (f"{min(years)}-01-01~{max(years) + 1}-12-31"
                   if years else "2020-01-01~2026-12-31")
        anns_by_cat: Dict[str, List[Dict]] = {}
        for cat in categories:
            anns = self.client.query_announcements(
                sec_code, org_id, _PERIODIC_CATEGORIES[cat], se_date)
            anns_by_cat[cat] = anns
            stats["queried"] += len(anns)
            for a in anns:
                self._upsert_announcement(meta, a, ts_code)
                stats["saved"] += 1
        store.save_stock_meta(ts_code, meta)

        if not download:
            return stats
        for year in years:
            for cat in categories:
                full_by_year = [
                    a for a in anns_by_cat.get(cat, [])
                    if parse_report_year(
                        a.get("announcementTitle", "")) == year]
                best = pick_full_report(full_by_year)
                if not best:
                    logger.warning(f"{ts_code} {year} {cat}: 无全文公告")
                    continue
                for k, v in self._download_one(meta, ts_code, year, cat,
                                               best, force).items():
                    stats[k] = stats.get(k, 0) + v
        store.save_stock_meta(ts_code, meta)
        return stats

    def download_from_meta(self, ts_code: str, years: List[int],
                           categories: Tuple[str, ...] = ("ndbg",),
                           force: bool = False) -> Dict[str, int]:
        """元数据已在盘: 按 (年,类) 挑全文直接下载, 零查询接口调用
        (查询接口限流敏感; PDF 链接来自 meta 落盘元数据)。"""
        stats = {"downloaded": 0, "skipped": 0, "failed": 0}
        meta = store.load_stock_meta(ts_code)
        for year in years:
            for cat in categories:
                cands = [dict(e, announcementId=aid,
                              announcementTitle=e.get("title") or "",
                              announcementTime=e.get("announce_ts"))
                         for aid, e in meta["announcements"].items()
                         if e.get("category") == cat
                         and e.get("report_year") == year
                         and e.get("adjunct_url")]
                best = pick_full_report(cands)
                if not best:
                    continue
                for k, v in self._download_one(meta, ts_code, year, cat,
                                               best, force).items():
                    stats[k] = stats.get(k, 0) + v
        store.save_stock_meta(ts_code, meta)
        return stats

    def _download_one(self, meta: Dict, ts_code: str, year: int, cat: str,
                      ann: Dict, force: bool) -> Dict[str, int]:
        """下载单份全文并回填状态。存在性检查: 文件在盘且未 force -> 跳过
        (文件即真相; PG 时代的存量 PDF 借此自愈回填 meta 状态)"""
        aid = str(ann.get("announcementId") or ann.get("adjunctUrl", ""))
        entry = meta["announcements"].get(aid) or {}
        dest = DOWNLOAD_ROOT / ts_code / f"{year}_{cat}.pdf"
        if dest.exists() and not force:
            logger.info(f"跳过 (已下载): {dest}")
            if entry.get("download_status") != "done":
                entry.update(download_status="done",
                             file_path=str(dest.resolve()),
                             file_size=dest.stat().st_size)
                meta["announcements"][aid] = entry
            return {"skipped": 1}

        ok, size = self.client.download_pdf(ann["adjunctUrl"], dest)
        if ok:
            entry.update(download_status="done", file_path=str(dest.resolve()),
                         file_size=size,
                         downloaded_at=datetime.now().isoformat())
            meta["announcements"][aid] = entry
            logger.info(f"已下载: {dest} ({size / 1e6:.1f}MB)")
            return {"downloaded": 1}
        entry.update(download_status="failed")
        meta["announcements"][aid] = entry
        return {"failed": 1}


def main() -> int:
    parser = argparse.ArgumentParser(description="巨潮定期报告下载")
    parser.add_argument("--stock", required=True,
                        help="证券代码, 逗号分隔多只 (如 300308 或 300308,600519.SH)")
    parser.add_argument("--years", default="",
                        help="报告年度, 逗号分隔 (如 2023,2024,2025); 缺省最近3年)")
    parser.add_argument("--no-download", action="store_true", help="只落盘元数据不下载")
    parser.add_argument("--all-periodic", action="store_true",
                        help="年报+半年报+一/三季报 (缺省仅年报)")
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    args = parser.parse_args()

    if args.years:
        years = [int(y) for y in args.years.split(",") if y.strip()]
    else:
        y = datetime.now().year
        years = [y - 2, y - 1, y]   # 报告年度; 跨年披露由宽窗覆盖
    cats = tuple(_PERIODIC_CATEGORIES) if args.all_periodic else ("ndbg",)

    total: Dict[str, int] = {}
    dl = CninfoDownloader()
    for s in args.stock.split(","):
        if not s.strip():
            continue
        stats = dl.sync_stock(s.strip(), years,
                              download=not args.no_download,
                              force=args.force, categories=cats)
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
        print(f"[{s}] {stats}")
    print(f"[TOTAL] {total}")
    return 0 if total.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
