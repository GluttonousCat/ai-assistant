# -*- encoding: utf-8 -*-
"""
研究生产线总装 CLI (T08) — 顺序函数编排, 图思维不引框架

    python -m skills.research 贵州茅台                     # 全流程: 采集→草稿→图表→渲染
    python -m skills.research 贵州茅台 --skip-evidence     # 复用已落盘分片, 跳过三源采集
    python -m skills.research 600519.SH --only render      # 只渲染 (需已有 draft.json)

各 Stage 模块 (T02/T03/T04/T06/T07) 由并行任务维护, 此处一律 lazy import:
未合并的 Stage 跳过并告警 (已落盘产物可继续用), 不阻塞可运行的后续阶段。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from core.logger import get_logger
from core.paths import PathManager

from skills.research.contracts import (
    Draft,
    load_draft,
    load_pack,
    research_dir,
    save_draft,
    save_shard,
    validate_shard,
)
from skills.research.render.render_md import render_report

logger = get_logger(__name__)

_TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)


def _output_root(out_root: str) -> Path:
    return Path(out_root) if out_root else Path(PathManager().output_dir)


# ---------- 股票定位 ----------

def _resolve_ts_code(stock: str, out_root: Path) -> Optional[str]:
    """名/代码 → ts_code。代码格式直接用; 名称则反查已落盘 draft.json 的 name 字段。"""
    s = stock.strip()
    if _TS_CODE_RE.match(s):
        return s.upper()
    root = out_root / "research"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            dj = d / "draft.json"
            if not dj.is_file():
                continue
            try:
                dr = load_draft(dj)
            except Exception:
                continue
            if dr.name == s or dr.ts_code == s:
                return dr.ts_code
    return None


def _require_research_dir(stock: str, out_root: Path) -> Path:
    ts_code = _resolve_ts_code(stock, out_root)
    rdir = research_dir(out_root, ts_code) if ts_code else None
    if not rdir or not rdir.is_dir():
        avail = [d.name for d in sorted((out_root / "research").glob("*"))
                 if d.is_dir()] if (out_root / "research").is_dir() else []
        raise SystemExit(
            f"未找到 {stock!r} 的研究目录 ({rdir or '代码无法解析'})。"
            f"现有目录: {avail or '无'}; 请先跑全流程, 或直接用 ts_code (如 600519.SH)。")
    return rdir


# ---------- Stage 1 · 三源采集 (T02/T03/T04) ----------

def stage_collect(stock: str, years: int, annual_years: List[int],
                  days: int, out_root: Path) -> Tuple[str, Path]:
    """三源采集 → 分片落盘。tushare 先行 (其内解析 名→代码); 各源独立容错。"""
    ts_code = _resolve_ts_code(stock, out_root)
    rdir = research_dir(out_root, ts_code) if ts_code else None

    try:
        from skills.research.sources.tushare_source import collect_tushare_facts
    except ImportError as e:
        logger.warning("T02 模块未合并 (sources/tushare_source), 跳过: %s", e)
        if not ts_code:
            raise SystemExit(f"无法解析 {stock!r} 的股票代码: 非代码格式, "
                             "T02 未合并且无历史 draft 可反查; 请直接传 ts_code。")
    else:
        shard = collect_tushare_facts(stock, years=years, out_dir=rdir)
        if not ts_code and not (rdir / "facts_tushare.json").is_file():
            # T02 以默认目录落盘 (如 --out-root 覆盖时), 转存一份到目标研究目录
            save_shard(shard, research_dir(out_root, shard.ts_code) / "facts_tushare.json")
        ts_code = ts_code or shard.ts_code
        rdir = rdir or research_dir(out_root, ts_code)
        for err in validate_shard(shard):
            logger.warning("[tushare] 分片校验: %s", err)
        logger.info("[tushare] facts=%d → %s", len(shard.facts), rdir)

    for year in annual_years:
        try:
            from skills.research.sources.annual_source import collect_annual_facts
        except ImportError as e:
            logger.warning("T03 模块未合并 (sources/annual_source), 跳过 %s 年报: %s",
                           year, e)
            break
        try:
            shard = collect_annual_facts(ts_code, year, out_dir=rdir)
            for err in validate_shard(shard):
                logger.warning("[annual %s] 分片校验: %s", year, err)
            logger.info("[annual %s] facts=%d", year, len(shard.facts))
        except Exception as e:                      # 单年报失败不阻塞其他源
            logger.error("[annual %s] 采集失败: %s", year, e)

    try:
        from skills.research.sources.report_source import collect_report_views
    except ImportError as e:
        logger.warning("T04 模块未合并 (sources/report_source), 跳过: %s", e)
    else:
        try:
            shard = collect_report_views(ts_code, days=days, out_dir=rdir)
            for err in validate_shard(shard):
                logger.warning("[reports] 分片校验: %s", err)
            logger.info("[reports] views=%d", len(shard.views))
        except Exception as e:
            logger.error("[reports] 采集失败: %s", e)

    return ts_code, rdir


# ---------- Stage 2 · 草稿 (T06) ----------

def stage_generate(ts_code: str, rdir: Path, years: int,
                   stock: str = "") -> Draft:
    try:
        from skills.research.compose.generator import generate_draft
    except ImportError as e:
        draft_path = rdir / "draft.json"
        if draft_path.is_file():
            logger.warning("T06 生成器未合并 (%s), 复用已落盘草稿: %s", e, draft_path)
            return load_draft(draft_path)
        raise SystemExit(f"T06 生成器未合并且无已落盘 draft.json, 无法生成草稿: {e}; "
                         "可先用 --only render 渲染已有产物。")
    draft = generate_draft(stock or ts_code, years=years, research_dir=rdir)
    if not (rdir / "draft.json").is_file():         # 兜底: 生成器漏存则代存
        save_draft(draft, rdir / "draft.json")
    logger.info("[generate] 章节 %d 章, flags %d 条, 假设 %d 条",
                len(draft.chapters), len(draft.flags), len(draft.hypotheses))
    return draft


# ---------- Stage 3 · 图表 (T07) 与渲染 (T08) ----------

def stage_figures(rdir: Path) -> List:
    """图表工场; 文件落 figs/, 渲染阶段按 chNN_ 文件名约定回收, 失败不阻塞渲染。"""
    try:
        from skills.research.compose.figures import build_figures
    except ImportError as e:
        logger.warning("T07 模块未合并 (compose/figures), 跳过出图: %s", e)
        return []
    try:
        draft = load_draft(rdir / "draft.json")
        facts, _views = load_pack(rdir)
        items = build_figures(draft, facts, rdir)
        logger.info("[figures] 产出 %d 张图", len(items))
        return items
    except Exception as e:
        logger.error("[figures] 出图失败 (不阻塞渲染): %s", e)
        return []


def stage_render(rdir: Path) -> Tuple[Path, Draft, list, list, list]:
    draft_path = rdir / "draft.json"
    if not draft_path.is_file():
        raise SystemExit(f"缺少 {draft_path}; 请先跑草稿阶段 (去掉 --only render)。")
    draft = load_draft(draft_path)
    facts, views = load_pack(rdir)
    figs = sorted((rdir / "figs").glob("*.png")) if (rdir / "figs").is_dir() else []
    out = rdir / f"report_{date.today().isoformat()}.md"
    render_report(draft, facts, views, figs, out)
    return out, draft, facts, views, figs


# ---------- 入口 ----------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m skills.research",
        description="研究生产线总装: 三源采集 → 十章草稿 → 图表 → 研究报告 MD")
    p.add_argument("stock", help="股票名或代码 (如 贵州茅台 / 600519.SH)")
    p.add_argument("--years", type=int, default=5, help="财务时序年数 (T02/T06)")
    p.add_argument("--annual-years", default="2025",
                   help="年报抽取年度, 逗号分隔 (默认 2025)")
    p.add_argument("--days", type=int, default=365, help="研报回看天数 (T04)")
    p.add_argument("--skip-evidence", action="store_true",
                   help="跳过三源采集, 复用已落盘分片")
    p.add_argument("--only", choices=["render"], help="只跑指定阶段 (render 需已有 draft.json)")
    p.add_argument("--out-root", default="", help="输出根目录 (默认 output/)")
    args = p.parse_args(argv)

    out_root = _output_root(args.out_root)
    annual_years = [int(y) for y in re.split(r"[,，\s]+", args.annual_years) if y.strip()]

    try:
        if args.only == "render":
            rdir = _require_research_dir(args.stock, out_root)
            out, draft, *_ = stage_render(rdir)
        else:
            ts_code, rdir = (None, None)
            if not args.skip_evidence:
                ts_code, rdir = stage_collect(args.stock, args.years,
                                              annual_years, args.days, out_root)
            ts_code = ts_code or _resolve_ts_code(args.stock, out_root)
            if not ts_code:
                raise SystemExit(f"无法确定 {args.stock!r} 的 ts_code, 中止。")
            rdir = rdir or research_dir(out_root, ts_code)
            stage_generate(ts_code, rdir, args.years, stock=args.stock)
            stage_figures(rdir)
            out, draft, *_ = stage_render(rdir)
    except SystemExit:
        raise
    except Exception:
        logger.exception("总装失败")
        return 1

    print(f"研究报告已生成: {out}")
    print(f"  {draft.name}（{draft.ts_code}）· {len(draft.chapters)} 章 · "
          f"证据分片汇总与脚注见文末来源区")
    return 0


if __name__ == "__main__":
    sys.exit(main())
