# -*- encoding: utf-8 -*-
"""
巨潮公告下载编排: 股票解析 -> 公告查询 -> 筛选 -> 元数据入库 -> PDF 下载

幂等 (重跑安全):
- 元数据: announcement_id (巨潮原生) 主键 upsert
- 下载:   file_path 已回填且文件在盘且未 --force -> 跳过
          (存在性检查而非水位线 — 下载完成顺序无序, 见 AGENTS.md 踩坑表)

用法:
    python -m tools.cninfo.downloader --stock 中际旭创 --years 2023,2024,2025
    python -m tools.cninfo.downloader --stock 300308,600519 --no-download
    python -m tools.cninfo.downloader --stock 中际旭创 --all-periodic   # 含半年报/季报
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger
from tools.cninfo.client import CninfoClient
from tools.cninfo.filters import classify_category, parse_report_year, pick_full_report

logger = get_logger(__name__)

DOWNLOAD_ROOT = Path("output/cninfo/downloads")

# 查询用类别参数: (类别码, 巨潮 category 参数, 披露窗口年份偏移)
_PERIODIC_CATEGORIES = {
    "ndbg": "category_ndbg_szsh",    # 年报: 次年 1-4 月披露
    "bndbg": "category_bndbg_szsh",  # 半年报: 当年 7-8 月
    "yjdbg": "category_yjdbg_szsh",  # 一季报: 当年 4 月
    "sjdbg": "category_sjdbg_szsh",  # 三季报: 当年 10 月
}


def _resolve_ts_code(stock: str) -> Optional[str]:
    """股票名/代码 -> ts_code (复用 fin_query 的映射)"""
    from skills.fin_query.skill import lookup_ts_code
    return lookup_ts_code(stock.strip())


class CninfoDownloader:
    def __init__(self, client: Optional[CninfoClient] = None):
        self.client = client or CninfoClient()

    # ---------- 元数据 ----------

    @staticmethod
    def _upsert_announcement(pg, ann: Dict, ts_code: str) -> None:
        """巨潮原始条目 -> fin.cninfo_announcement (保留已有下载状态列)"""
        from datetime import datetime
        title = ann.get("announcementTitle") or ""
        aid = str(ann.get("announcementId") or ann.get("adjunctUrl", ""))
        ts = ann.get("announcementTime")
        announce_date = (datetime.fromtimestamp(ts / 1000).date().isoformat()
                         if ts else None)
        pg.execute(
            """
            INSERT INTO fin.cninfo_announcement
                (announcement_id, ts_code, sec_code, sec_name, title, category,
                 report_year, announce_date, adjunct_url, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (announcement_id) DO UPDATE SET
                title = EXCLUDED.title,
                category = EXCLUDED.category,
                report_year = EXCLUDED.report_year,
                announce_date = EXCLUDED.announce_date,
                updated_at = now()
            """,
            (aid, ts_code, ann.get("secCode"), ann.get("secName"), title,
             classify_category(title), parse_report_year(title),
             announce_date, ann.get("adjunctUrl")))

    # ---------- 主流程 ----------

    def sync_stock(self, stock: str, years: List[int], download: bool = True,
                   force: bool = False, categories: Tuple[str, ...] = ("ndbg",)
                   ) -> Dict[str, int]:
        """同步一只股票的定期报告: 查询->筛选->入库->(可选)下载。返回统计。"""
        from storage.pg import PgClient
        stats = {"queried": 0, "saved": 0, "downloaded": 0,
                 "skipped": 0, "failed": 0}
        ts_code = _resolve_ts_code(stock)
        if not ts_code:
            logger.error(f"未识别股票: {stock}")
            stats["failed"] += 1
            return stats
        sec_code = ts_code.split(".")[0]
        org_id = self.client.resolve_org_id(sec_code)
        if not org_id:
            logger.error(f"巨潮未找到 orgId: {sec_code} ({stock})")
            stats["failed"] += 1
            return stats
        logger.info(f"{stock} -> {ts_code} (orgId={org_id}), 年份 {years}")

        from storage.pg_schema import DDL_CNINFO_ANNOUNCEMENT
        with PgClient() as pg:
            pg.execute(DDL_CNINFO_ANNOUNCEMENT)   # 幂等建表 (独立于业务事务)
            pg.conn.commit()
            # 每类别一次宽窗查询 (覆盖全部年份, 5年4类=4次请求而非20次;
            # 分号多类别参数实测不可用), 入库全部, 下载按 (year,cat) 挑全文
            se_date = (f"{min(years)}-01-01~{max(years) + 1}-12-31"
                       if years else "2020-01-01~2026-12-31")
            anns_by_cat: Dict[str, List[Dict]] = {}
            for cat in categories:
                anns = self.client.query_announcements(
                    sec_code, org_id, _PERIODIC_CATEGORIES[cat], se_date)
                anns_by_cat[cat] = anns
                stats["queried"] += len(anns)
                for a in anns:
                    self._upsert_announcement(pg, a, ts_code)
                    stats["saved"] += 1
                pg.conn.commit()

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
                    stats.update(self._download_one(pg, ts_code, year, cat,
                                                    best, force))
        return stats

    def _download_one(self, pg, ts_code: str, year: int, cat: str,
                      ann: Dict, force: bool) -> Dict[str, int]:
        """下载单份全文并回填状态。存在性检查: 已 done 且文件在盘且未 force -> 跳过"""
        aid = str(ann.get("announcementId") or ann.get("adjunctUrl", ""))
        row = pg.fetch_one(
            "SELECT file_path, download_status FROM fin.cninfo_announcement "
            "WHERE announcement_id = %s", (aid,))
        dest = DOWNLOAD_ROOT / ts_code / f"{year}_{cat}.pdf"
        if (row and row["download_status"] == "done" and row["file_path"]
                and Path(row["file_path"]).exists() and not force):
            logger.info(f"跳过 (已下载): {dest}")
            return {"skipped": 1}

        ok, size = self.client.download_pdf(ann["adjunctUrl"], dest)
        from datetime import datetime
        if ok:
            pg.execute(
                "UPDATE fin.cninfo_announcement SET file_path=%s, file_size=%s, "
                "download_status='done', error_msg=NULL, downloaded_at=%s, "
                "updated_at=now() WHERE announcement_id=%s",
                (str(dest.resolve()), size, datetime.now(), aid))
            pg.conn.commit()
            logger.info(f"已下载: {dest} ({size / 1e6:.1f}MB)")
            return {"downloaded": 1}
        pg.execute(
            "UPDATE fin.cninfo_announcement SET download_status='failed', "
            "error_msg='download failed', updated_at=now() WHERE announcement_id=%s",
            (aid,))
        pg.conn.commit()
        return {"failed": 1}


def main() -> int:
    parser = argparse.ArgumentParser(description="巨潮定期报告下载")
    parser.add_argument("--stock", required=True,
                        help="股票名或代码, 逗号分隔多只 (如 中际旭创 或 300308,600519)")
    parser.add_argument("--years", default="",
                        help="报告年度, 逗号分隔 (如 2023,2024,2025); 缺省最近3年)")
    parser.add_argument("--no-download", action="store_true", help="只入库元数据不下载")
    parser.add_argument("--all-periodic", action="store_true",
                        help="年报+半年报+一/三季报 (缺省仅年报)")
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    args = parser.parse_args()

    if args.years:
        years = [int(y) for y in args.years.split(",") if y.strip()]
    else:
        from datetime import date
        y = date.today().year
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
