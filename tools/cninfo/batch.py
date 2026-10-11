# -*- encoding: utf-8 -*-
"""
巨潮全市场批量爬取 (元数据为主, PDF 按需)

定位与量级 (2026-09 实测口径):
- 全市场 ~5400 只 × 5 年 × 4 类定期报告 ≈ 10 万条公告元数据
- PDF 全量 ≈ 300GB, 本机磁盘放不下且消费端不成立 → **元数据全量 + PDF 按需**
  (关注池批量下载 / 对话内 fetch_annual_report 单只拉取)

限流现实 (实测短时高频即 504/错误页):
- 每类别宽窗一次查询 (5年4类 = 4 请求/股)
- 股票间 2~5s 随机延迟; 连续 CONSECUTIVE_FAIL_LIMIT 只失败 → 判定站点限流,
  优雅停止 (进度已落盘, 重跑自动续)
- --max-minutes 时间预算, 到点优雅停 (分夜跑)

断点续传: output/cninfo/meta/<ts_code>.json 的 state 按股票记录状态;
重跑跳过 done 的股票 (股票枚举天然有序, 股票级状态非"下载完成顺序≠ID顺序"
的水位线坑)

用法:
    python -m tools.cninfo.batch --years 2021,2022,2023,2024,2025           # 元数据全量
    python -m tools.cninfo.batch --limit 20                                  # 试跑 20 只
    python -m tools.cninfo.batch --max-minutes 120                           # 每晚 2h 分夜跑
    python -m tools.cninfo.batch --download-watchlist 300308,600519 --years 2024
    python -m tools.cninfo.batch --download-index 000300.SH --years 2024     # 指数成分补 PDF
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import store
from .client import CninfoClient
from .downloader import CninfoDownloader, _PERIODIC_CATEGORIES
from .filters import normalize_ts_code

logger = logging.getLogger(__name__)

CONSECUTIVE_FAIL_LIMIT = 8      # 连续 N 只失败 → 站点限流, 优雅停止
BATCH_MIN_DELAY = 2.0           # 批量模式股票间最小延迟 (秒)
BATCH_MAX_DELAY = 5.0


def _tushare_token() -> str:
    """TUSHARE_TOKEN: 进程 env 优先, 其次 cwd 的 .env 轻量读取 (免配置框架依赖)"""
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
    """全市场清单: 巨潮自家 szse_stock.json (code/orgId/zwjc, 前端搜索下拉数据源)。

    ts_code 升序 (断点续传枚举稳定); 推不出交易所后缀的条目 (基金/债券等) 跳过。
    """
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
    """指数成分股 (如 000300.SH 沪深300 / 000905.SH 中证500)。

    tushare index_weight 为月度快照, 取最近 95 天内最新一期的全部成分。
    """
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
        # 批量模式: 更克制的延迟
        self.client = client or CninfoClient(min_delay=BATCH_MIN_DELAY,
                                             max_delay=BATCH_MAX_DELAY,
                                             retries=3)
        self.dl = CninfoDownloader(client=self.client)

    # ---------- 进度状态 (store 文件, 单股 json 的 state 键) ----------

    @staticmethod
    def _get_state(ts_code: str) -> Dict:
        return store.load_stock_meta(ts_code).get("state") or {}

    @staticmethod
    def _set_state(ts_code: str, meta_status: str,
                   error: Optional[str] = None,
                   dl_status: Optional[str] = None) -> None:
        """dl_status 仅在传入时更新 (元数据 pass 不动下载状态)"""
        meta = store.load_stock_meta(ts_code)
        st = meta.setdefault("state", {})
        st["meta"] = meta_status
        if dl_status:
            st["dl"] = dl_status
        st["error"] = (error or "")[:500] or None
        store.save_stock_meta(ts_code, meta)

    # ---------- 主流程 ----------

    def _process_stock(self, stock: Dict[str, str], i: int, n: int,
                       years: List[int], cats, with_pdf: bool,
                       resume: bool, total_stats: Dict[str, int]) -> str:
        """处理单只股票 (幂等可重入)。返回 skip/done/error; 计数进 total_stats。"""
        ts_code, name = stock["ts_code"], stock["name"]

        if resume:
            state = self._get_state(ts_code)
            if state.get("meta") == "done":
                if not with_pdf or state.get("dl") == "done":
                    total_stats["stocks_skip"] += 1
                    return "skip"
                # meta 已 done 仅缺 PDF: 直读 meta 下载, 免重打查询接口
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
        """全市场/指数成分 爬取 (断点续传 + 限流熔断 + 时间预算)。

        with_pdf=False: 仅元数据 (全量形态);
        with_pdf=True:  元数据 + 每股 5年×4类 全文 PDF 下载 (关注池形态,
                        沪深300 ≈ 6000 份 ≈ 15-20GB)。
        断点判定: 元数据模式看 meta 状态; PDF 模式须 meta+dl 双 done。
        """
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

    def download_index_pdfs(self, index_code: str, years: List[int],
                            categories: List[str] = None,
                            max_minutes: float = 0) -> Dict[str, int]:
        """指数成分补 PDF: 元数据已在盘的成分股, 免查询接口直接按 meta 链接下载。

        适合"元数据已批量爬完、再补 PDF"的两段式 (查询接口限流敏感, 此路零查询);
        只下"报告全文" (filters 筛选), 摘要等干扰版本留痕不下载。
        """
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
    parser = argparse.ArgumentParser(description="巨潮全市场批量爬取")
    parser.add_argument("--years", default="2021,2022,2023,2024,2025",
                        help="报告年度, 逗号分隔 (缺省最近5年)")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只 (试跑)")
    parser.add_argument("--fresh", action="store_true",
                        help="忽略进度全量重跑 (缺省断点续传)")
    parser.add_argument("--max-minutes", type=float, default=0,
                        help="时间预算(分钟), 到点优雅停 (分夜跑)")
    parser.add_argument("--download-watchlist", default="",
                        help="只对关注池下载 PDF (逗号分隔代码), 不做全量爬取")
    parser.add_argument("--index", default="",
                        help="只爬指数成分股 (如 000300.SH 沪深300 / 000905.SH 中证500)")
    parser.add_argument("--with-pdf", action="store_true",
                        help="连带下载全文 PDF (关注池形态; 缺省仅元数据)")
    parser.add_argument("--download-index", default="",
                        help="指数成分补 PDF: 元数据已在盘的成分股直接下载 "
                             "(如 000300.SH, 免查询接口)")
    args = parser.parse_args()

    years = [int(y) for y in args.years.split(",") if y.strip()]
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
