# -*- encoding: utf-8 -*-

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import store
from .client import CninfoClient
from .filters import (classify_category, normalize_ts_code, parse_report_year,
                      pick_full_report)

logger = logging.getLogger(__name__)

DOWNLOAD_ROOT = Path("output/cninfo/downloads")

_PERIODIC_CATEGORIES = {
    "ndbg": "category_ndbg_szsh",    # annual report: disclosed Jan-Apr of the next year
    "bndbg": "category_bndbg_szsh",  # semi-annual report: Jul-Aug of the same year
    "yjdbg": "category_yjdbg_szsh",  # Q1 report: Apr of the same year
    "sjdbg": "category_sjdbg_szsh",  # Q3 report: Oct of the same year
}

_DL_FIELDS = ("file_path", "file_size", "download_status", "downloaded_at")

CONSECUTIVE_FAIL_LIMIT = 8
BATCH_MIN_DELAY = 2.0
BATCH_MAX_DELAY = 5.0


class CninfoDownloader:
    def __init__(self, client: Optional[CninfoClient] = None):
        self.client = client or CninfoClient()

    @staticmethod
    def _upsert_announcement(meta: Dict, ann: Dict, ts_code: str) -> None:
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
            "announce_date": (
                datetime.fromtimestamp(ts / 1000).date().isoformat() if ts
                else None
            ),
            "announce_ts": ts,
            "adjunct_url": ann.get("adjunctUrl"),
        }
        entry.update({k: v for k, v in (meta["announcements"].get(aid) or {}).items()
                      if k in _DL_FIELDS})
        meta["announcements"][aid] = entry

    # ---------- orgId ----------

    def _resolve_org_id(self, sec_code: str) -> Optional[str]:
        org_map = store.load_org_map()
        if sec_code in org_map:
            return org_map[sec_code]
        org_id = self.client.resolve_org_id(sec_code)
        if org_id:
            org_map[sec_code] = org_id
            store.save_org_map(org_map)
        return org_id

    def sync_stock(
            self, stock: str, years: List[int], download: bool = True,
            force: bool = False, categories: Tuple[str, ...] = ("ndbg",)
    ) -> Dict[str, int]:
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


def _tushare_token() -> str:
    tok = os.environ.get("TUSHARE_TOKEN", "")
    if tok:
        return tok
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TUSHARE_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _load_universe(client: CninfoClient) -> List[Dict[str, str]]:
    items = client.fetch_stock_list()
    if not items:
        raise RuntimeError("巨潮全市场清单不可达 (szse_stock.json), 无法全量枚举")
    out = []
    for it in items:
        ts_code = normalize_ts_code(it.get("code") or "")
        if ts_code:
            out.append({"ts_code": ts_code, "name": it.get("zwjc") or ""})
    out.sort(key=lambda s: s["ts_code"])
    logger.info(f"全市场清单: {len(out)} 只 A 股 (共 {len(items)} 条)")
    return out


def _load_index_universe(index_code: str) -> List[Dict[str, str]]:
    import tushare as ts
    from datetime import date, timedelta

    tok = _tushare_token()
    if not tok:
        raise RuntimeError("按指数成分爬取需 TUSHARE_TOKEN (进程 env 或 cwd .env)")
    pro = ts.pro_api(tok)
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=95)).strftime("%Y%m%d")
    df = pro.index_weight(index_code=index_code, start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError(f"tushare index_weight 无数据: {index_code} "
                           f"({start}~{end})")
    latest = df["trade_date"].max()
    codes = sorted(df[df["trade_date"] == latest]["con_code"].unique())
    logger.info(f"{index_code} 成分股: {len(codes)} 只 (快照期 {latest})")
    return [{"ts_code": c, "name": ""} for c in codes]


class BatchCrawler:
    def __init__(self, client: Optional[CninfoClient] = None):
        self.client = client or CninfoClient(min_delay=BATCH_MIN_DELAY,
                                             max_delay=BATCH_MAX_DELAY,
                                             retries=3)
        self.dl = CninfoDownloader(client=self.client)

    @staticmethod
    def _get_state(ts_code: str) -> Dict:
        return store.load_stock_meta(ts_code).get("state") or {}

    @staticmethod
    def _set_state(
            ts_code: str, meta_status: str,
            error: Optional[str] = None,
            dl_status: Optional[str] = None
    ) -> None:
        meta = store.load_stock_meta(ts_code)
        st = meta.setdefault("state", {})
        st["meta"] = meta_status
        if dl_status:
            st["dl"] = dl_status
        st["error"] = (error or "")[:500] or None
        store.save_stock_meta(ts_code, meta)

    # ---------- main flow ----------

    def _process_stock(
            self, stock: Dict[str, str], i: int, n: int,
            years: List[int], cats, with_pdf: bool,
            resume: bool, total_stats: Dict[str, int]
    ) -> str:
        ts_code, name = stock["ts_code"], stock["name"]

        if resume:
            state = self._get_state(ts_code)
            if state.get("meta") == "done":
                if not with_pdf or state.get("dl") == "done":
                    total_stats["stocks_skip"] += 1
                    return "skip"
                # meta already done, only PDF missing: download straight from
                # meta, skip re-hitting the query API
                dl = self.dl.download_from_meta(ts_code, years, tuple(cats))
                total_stats["downloaded"] += dl.get("downloaded", 0)
                total_stats["dl_skipped"] += dl.get("skipped", 0)
                total_stats["pdf_failed"] = (
                    total_stats.get("pdf_failed", 0) + dl.get("failed", 0))
                logger.info(f"[{i}/{n}] {ts_code} {name} "
                            f"直读下载: {dl.get('downloaded', 0)} 新增 "
                            f"+ {dl.get('skipped', 0)} 已有"
                            + (f", {dl['failed']} 失败"
                               if dl.get("failed") else ""))
                self._set_state(ts_code, "done",
                                dl_status="done"
                                if not dl.get("failed") else "partial")
                total_stats["stocks_done"] += 1
                return "done"

        stats = self.dl.sync_stock(ts_code, years, download=with_pdf,
                                   categories=tuple(cats))
        if stats.get("failed"):
            self._set_state(ts_code, "error", "orgId/query failed (限流?)")
            total_stats["stocks_err"] += 1
            return "error"
        self._set_state(ts_code, "done",
                        dl_status="done" if with_pdf else None)
        total_stats["stocks_done"] += 1
        total_stats["queried"] += stats.get("queried", 0)
        total_stats["saved"] += stats.get("saved", 0)
        total_stats["downloaded"] += stats.get("downloaded", 0)
        total_stats["dl_skipped"] += stats.get("skipped", 0)
        return "done"

    def run(self, years: List[int], limit: int = 0, resume: bool = True,
            max_minutes: float = 0, categories: List[str] = None,
            index_code: str = "", with_pdf: bool = False) -> Dict[str, int]:
        cats = categories or list(_PERIODIC_CATEGORIES.keys())
        universe = (_load_index_universe(index_code) if index_code
                    else _load_universe(self.client))
        total_stats = {"stocks_done": 0, "stocks_skip": 0, "stocks_err": 0,
                       "queried": 0, "saved": 0, "downloaded": 0,
                       "dl_skipped": 0}
        t0 = time.time()
        consecutive_fails = 0
        processed = 0

        for i, stock in enumerate(universe, 1):
            if limit and processed >= limit:
                break
            if max_minutes and (time.time() - t0) > max_minutes * 60:
                logger.info(f"时间预算 {max_minutes}min 用尽, 优雅停止 "
                            f"(已处理 {processed}/{len(universe)})")
                break
            ts_code, name = stock["ts_code"], stock["name"]

            status = self._process_stock(stock, i, len(universe), years,
                                         cats, with_pdf, resume, total_stats)
            processed += 0 if status == "skip" else 1

            if status == "error":
                consecutive_fails += 1
                logger.warning(f"[{i}/{len(universe)}] {ts_code} {name} "
                               f"失败 (连续 {consecutive_fails})")
                if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                    logger.error(f"连续 {consecutive_fails} 只失败, "
                                 f"判定站点限流 — 优雅停止, 进度已保存, 稍后重跑续传")
                    break
            else:
                consecutive_fails = 0
                if processed % 20 == 0 or processed <= 3:
                    el = time.time() - t0
                    rate = processed / el * 3600 if el else 0
                    eta_h = (len(universe) - i) / rate if rate else -1
                    logger.info(f"[{i}/{len(universe)}] 进度 {processed} 只, "
                                f"累计下载 {total_stats['downloaded']} 份 "
                                f"({rate:.0f} 只/h, 预计剩余 {eta_h:.1f}h)")

        total_stats["elapsed_min"] = round((time.time() - t0) / 60, 1)
        return total_stats

    def download_watchlist(self, stocks: List[str], years: List[int],
                           categories: List[str] = None) -> Dict[str, int]:
        """Batch PDF download for the watchlist (metadata already complete;
        just pick the full texts to download)."""
        cats = tuple(categories) if categories else ("ndbg",)
        total = {}
        for s in stocks:
            stats = self.dl.sync_stock(s, years, download=True,
                                       categories=cats)
            for k, v in stats.items():
                total[k] = total.get(k, 0) + v
            logger.info(f"关注池下载 {s}: {stats}")
        return total

    def download_index_pdfs(self, index_code: str, years: List[int],
                            categories: List[str] = None,
                            max_minutes: float = 0) -> Dict[str, int]:
        cats = tuple(categories) if categories else ("ndbg",)
        constituents = {s["ts_code"] for s in _load_index_universe(index_code)}
        todo = sorted(p.stem for p in store.META_ROOT.glob("*.json")
                      if p.stem in constituents)
        logger.info(f"指数补 PDF: {len(todo)} 只有元数据 "
                    f"({index_code} {years} {cats})")
        total = {"downloaded": 0, "skipped": 0, "failed": 0}
        t0 = time.time()

        for i, ts_code in enumerate(todo, 1):
            if max_minutes and (time.time() - t0) > max_minutes * 60:
                logger.info(f"时间预算用尽, 优雅停止 ({i - 1}/{len(todo)})")
                break
            stats = self.dl.download_from_meta(ts_code, years, cats)
            for k, v in stats.items():
                total[k] = total.get(k, 0) + v
            if i % 20 == 0:
                logger.info(f"下载进度 {i}/{len(todo)}: {total}")
        total["elapsed_min"] = round((time.time() - t0) / 60, 1)
        return total


def main() -> int:
    parser = argparse.ArgumentParser(description="巨潮定期报告下载 (单股/批量单入口)")
    # --- single-stock mode ---
    parser.add_argument("--stock", default="",
                        help="证券代码, 逗号分隔多只 (如 300308 或 300308,600519.SH); "
                             "缺省进入批量模式")
    parser.add_argument("--no-download", action="store_true",
                        help="(单股) 只落盘元数据不下载")
    parser.add_argument("--all-periodic", action="store_true",
                        help="(单股) 年报+半年报+一/三季报 (缺省仅年报)")
    parser.add_argument("--force", action="store_true", help="(单股) 强制重新下载")
    # --- batch mode (active when --stock is absent) ---
    parser.add_argument("--years", default="",
                        help="报告年度, 逗号分隔 (缺省: 单股最近3年 / 批量2021-2025)")
    parser.add_argument("--limit", type=int, default=0,
                        help="(批量) 只处理前 N 只 (试跑)")
    parser.add_argument("--fresh", action="store_true",
                        help="(批量) 忽略进度全量重跑 (缺省断点续传)")
    parser.add_argument("--max-minutes", type=float, default=0,
                        help="(批量) 时间预算(分钟), 到点优雅停 (分夜跑)")
    parser.add_argument("--index", default="",
                        help="(批量) 只爬指数成分股 (如 000300.SH / 000905.SH)")
    parser.add_argument("--with-pdf", action="store_true",
                        help="(批量) 连带下载全文 PDF (缺省仅元数据)")
    parser.add_argument("--download-watchlist", default="",
                        help="(批量) 只对关注池下载 PDF (逗号分隔代码), 不做全量爬取")
    parser.add_argument("--download-index", default="",
                        help="(批量) 指数成分补 PDF: 元数据已在盘直接下载 (如 000300.SH)")
    args = parser.parse_args()

    single = bool(args.stock.strip())
    if args.years:
        years = [int(y) for y in args.years.split(",") if y.strip()]
    elif single:
        y = datetime.now().year
        years = [y - 2, y - 1, y]   # report years; cross-year disclosure is covered by the wide window
    else:
        years = [2021, 2022, 2023, 2024, 2025]   # batch default: last 5 years

    if single:
        total: Dict[str, int] = {}
        dl = CninfoDownloader()
        cats = tuple(_PERIODIC_CATEGORIES) if args.all_periodic else ("ndbg",)
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

    bc = BatchCrawler()
    if args.download_index:
        stats = bc.download_index_pdfs(args.download_index, years,
                                       max_minutes=args.max_minutes)
        print(f"[DOWNLOAD-INDEX] {stats}")
        return 0
    if args.download_watchlist:
        stocks = [s for s in args.download_watchlist.split(",") if s.strip()]
        stats = bc.download_watchlist(stocks, years)
        print(f"[WATCHLIST] {stats}")
        return 0
    stats = bc.run(years, limit=args.limit, resume=not args.fresh,
                   max_minutes=args.max_minutes, index_code=args.index,
                   with_pdf=args.with_pdf)
    print(f"[BATCH] {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
