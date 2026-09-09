# -*- encoding: utf-8 -*-
"""
巨潮全市场批量爬取 (元数据为主, PDF 按需)

定位与量级 (2026-09 实测口径):
- 全市场 ~5400 只 × 5 年 × 4 类定期报告 ≈ 10 万条公告元数据 (PG 毫无压力)
- PDF 全量 ≈ 300GB, 本机磁盘放不下且消费端不成立 → **元数据全量 + PDF 按需**
  (关注池批量下载 / 对话内 fetch_annual_report 单只拉取)

限流现实 (实测短时高频即 504/错误页):
- 每类别宽窗一次查询 (5年4类 = 4 请求/股)
- 股票间 2~5s 随机延迟; 连续 CONSECUTIVE_FAIL_LIMIT 只失败 → 判定站点限流,
  优雅停止 (进度已落库, 重跑自动续)
- --max-minutes 时间预算, 到点优雅停 (分夜跑)

断点续传: fin.cninfo_sync_state 按股票记录 meta 状态; 重跑跳过 done 的股票
(股票枚举天然有序, 股票级状态非"下载完成顺序≠ID顺序"的水位线坑)

用法:
    python -m tools.cninfo.batch --years 2021,2022,2023,2024,2025           # 元数据全量
    python -m tools.cninfo.batch --limit 20                                  # 试跑 20 只
    python -m tools.cninfo.batch --max-minutes 120                           # 每晚 2h 分夜跑
    python -m tools.cninfo.batch --download-watchlist 300308,600519 --years 2024
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Dict, List, Optional

from core.logger import get_logger
from tools.cninfo.client import CninfoClient
from tools.cninfo.downloader import (CninfoDownloader, _PERIODIC_CATEGORIES)
from storage.pg_schema import (DDL_CNINFO_ANNOUNCEMENT, DDL_CNINFO_SYNC_STATE)

logger = get_logger(__name__)

CONSECUTIVE_FAIL_LIMIT = 8      # 连续 N 只失败 → 站点限流, 优雅停止
BATCH_MIN_DELAY = 2.0           # 批量模式股票间最小延迟 (秒)
BATCH_MAX_DELAY = 5.0


def _load_universe() -> List[Dict[str, str]]:
    """在市 A 股清单 (ts_code 升序, 保证断点续传枚举稳定)。

    stock_basic.status 当前全为 NULL (未回填退市标记) — 以 delist_date
    为空/未到 期 界定在市, 上线后有退市数据后可收紧。
    """
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT ts_code, name FROM stock.stock_basic "
            "WHERE delist_date IS NULL OR delist_date > current_date "
            "ORDER BY ts_code")
    return [{"ts_code": r["ts_code"], "name": r["name"]} for r in rows]


def _load_index_universe(index_code: str) -> List[Dict[str, str]]:
    """指数成分股 (如 000300.SH 沪深300 / 000905.SH 中证500)。

    tushare index_weight 为月度快照, 取最近 95 天内最新一期的全部成分。
    """
    import tushare as ts
    from datetime import date, timedelta
    from core.config import get_config
    from storage.pg import PgClient

    pro = ts.pro_api(get_config().tushare_token)
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=95)).strftime("%Y%m%d")
    df = pro.index_weight(index_code=index_code, start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError(f"tushare index_weight 无数据: {index_code} "
                           f"({start}~{end})")
    latest = df["trade_date"].max()
    codes = sorted(df[df["trade_date"] == latest]["con_code"].unique())
    logger.info(f"{index_code} 成分股: {len(codes)} 只 (快照期 {latest})")

    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT ts_code, name FROM stock.stock_basic WHERE ts_code = ANY(%s)",
            (codes,))
    by_code = {r["ts_code"]: r["name"] for r in rows}
    return [{"ts_code": c, "name": by_code.get(c, "")} for c in codes]


class BatchCrawler:
    def __init__(self, client: Optional[CninfoClient] = None):
        # 批量模式: 更克制的延迟
        self.client = client or CninfoClient(min_delay=BATCH_MIN_DELAY,
                                             max_delay=BATCH_MAX_DELAY,
                                             retries=3)
        self.dl = CninfoDownloader(client=self.client)

    # ---------- 进度表 ----------

    @staticmethod
    def _ensure_state_table(pg) -> None:
        pg.execute(DDL_CNINFO_SYNC_STATE)
        pg.conn.commit()

    @staticmethod
    def _get_state(pg, ts_code: str) -> Optional[Dict]:
        return pg.fetch_one(
            "SELECT meta_status, dl_status FROM fin.cninfo_sync_state "
            "WHERE ts_code=%s", (ts_code,))

    @staticmethod
    def _set_state(pg, ts_code: str, meta_status: str,
                   error: Optional[str] = None,
                   dl_status: Optional[str] = None) -> None:
        """dl_status 仅在传入时更新 (元数据 pass 不动下载状态)"""
        if dl_status:
            pg.execute(
                """
                INSERT INTO fin.cninfo_sync_state (ts_code, meta_status, dl_status, last_error, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (ts_code) DO UPDATE SET
                    meta_status = EXCLUDED.meta_status,
                    dl_status = EXCLUDED.dl_status,
                    last_error = EXCLUDED.last_error, updated_at = now()
                """, (ts_code, meta_status, dl_status, (error or "")[:500] or None))
        else:
            pg.execute(
                """
                INSERT INTO fin.cninfo_sync_state (ts_code, meta_status, last_error, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (ts_code) DO UPDATE SET
                    meta_status = EXCLUDED.meta_status,
                    last_error = EXCLUDED.last_error, updated_at = now()
                """, (ts_code, meta_status, (error or "")[:500] or None))
        pg.conn.commit()

    # ---------- 主流程 ----------

    def run(self, years: List[int], limit: int = 0, resume: bool = True,
            max_minutes: float = 0, categories: List[str] = None,
            index_code: str = "", with_pdf: bool = False) -> Dict[str, int]:
        """全市场/指数成分 爬取 (断点续传 + 限流熔断 + 时间预算)。

        with_pdf=False: 仅元数据 (全量形态);
        with_pdf=True:  元数据 + 每股 5年×4类 全文 PDF 下载 (关注池形态,
                        沪深300 ≈ 6000 份 ≈ 15-20GB)。
        断点判定: 元数据模式看 meta_status; PDF 模式须 meta+dl 双 done。
        """
        from storage.pg import PgClient
        cats = categories or list(_PERIODIC_CATEGORIES.keys())
        universe = (_load_index_universe(index_code) if index_code
                    else _load_universe())
        total_stats = {"stocks_done": 0, "stocks_skip": 0, "stocks_err": 0,
                       "queried": 0, "saved": 0, "downloaded": 0,
                       "dl_skipped": 0}
        t0 = time.time()
        consecutive_fails = 0
        processed = 0

        with PgClient() as pg:
            self._ensure_state_table(pg)
            pg.execute(DDL_CNINFO_ANNOUNCEMENT)
            pg.conn.commit()

            for i, stock in enumerate(universe, 1):
                if limit and processed >= limit:
                    break
                if max_minutes and (time.time() - t0) > max_minutes * 60:
                    logger.info(f"时间预算 {max_minutes}min 用尽, 优雅停止 "
                                f"(已处理 {processed}/{len(universe)})")
                    break
                ts_code, name = stock["ts_code"], stock["name"]

                if resume:
                    state = self._get_state(pg, ts_code)
                    if state and state["meta_status"] == "done" and (
                            not with_pdf or state["dl_status"] == "done"):
                        total_stats["stocks_skip"] += 1
                        continue

                stats = self.dl.sync_stock(ts_code, years, download=with_pdf,
                                           categories=tuple(cats))
                processed += 1
                if stats.get("failed"):
                    self._set_state(pg, ts_code, "error",
                                    "orgId/query failed (限流?)")
                    total_stats["stocks_err"] += 1
                    consecutive_fails += 1
                    logger.warning(f"[{i}/{len(universe)}] {ts_code} {name} "
                                   f"失败 (连续 {consecutive_fails})")
                    if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                        logger.error(f"连续 {consecutive_fails} 只失败, "
                                     f"判定站点限流 — 优雅停止, 进度已保存, 稍后重跑续传")
                        break
                else:
                    self._set_state(pg, ts_code, "done",
                                    dl_status="done" if with_pdf else None)
                    total_stats["stocks_done"] += 1
                    total_stats["queried"] += stats.get("queried", 0)
                    total_stats["saved"] += stats.get("saved", 0)
                    total_stats["downloaded"] += stats.get("downloaded", 0)
                    total_stats["dl_skipped"] += stats.get("skipped", 0)
                    consecutive_fails = 0
                    dl_note = (f", 下载 {stats.get('downloaded', 0)}"
                               f"+skip{stats.get('skipped', 0)}")
                    if processed % 20 == 0 or processed <= 3:
                        el = time.time() - t0
                        rate = processed / el * 3600 if el else 0
                        eta_h = (len(universe) - i) / rate if rate else -1
                        logger.info(f"[{i}/{len(universe)}] 进度 {processed} 只{dl_note} "
                                    f"({rate:.0f} 只/h, 预计剩余 {eta_h:.1f}h)")

        total_stats["elapsed_min"] = round((time.time() - t0) / 60, 1)
        return total_stats

    def download_watchlist(self, stocks: List[str], years: List[int],
                           categories: List[str] = None) -> Dict[str, int]:
        """关注池 PDF 批量下载 (元数据已全量, 直接挑全文下载)"""
        cats = tuple(categories) if categories else ("ndbg",)
        total = {}
        for s in stocks:
            stats = self.dl.sync_stock(s, years, download=True,
                                       categories=cats)
            for k, v in stats.items():
                total[k] = total.get(k, 0) + v
            logger.info(f"关注池下载 {s}: {stats}")
        return total


def main() -> int:
    parser = argparse.ArgumentParser(description="巨潮全市场批量爬取")
    parser.add_argument("--years", default="2021,2022,2023,2024,2025",
                        help="报告年度, 逗号分隔 (缺省最近5年)")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只 (试跑)")
    parser.add_argument("--fresh", action="store_true",
                        help="忽略进度表全量重跑 (缺省断点续传)")
    parser.add_argument("--max-minutes", type=float, default=0,
                        help="时间预算(分钟), 到点优雅停 (分夜跑)")
    parser.add_argument("--download-watchlist", default="",
                        help="只对关注池下载 PDF (逗号分隔), 不做全量爬取")
    parser.add_argument("--index", default="",
                        help="只爬指数成分股 (如 000300.SH 沪深300 / 000905.SH 中证500)")
    parser.add_argument("--with-pdf", action="store_true",
                        help="连带下载全文 PDF (关注池形态; 缺省仅元数据)")
    args = parser.parse_args()

    years = [int(y) for y in args.years.split(",") if y.strip()]
    bc = BatchCrawler()

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
