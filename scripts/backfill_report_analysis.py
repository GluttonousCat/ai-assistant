# -*- encoding: utf-8 -*-
"""
研报存量积压一次性回填 (逐篇流水线上线后, 存量记录仍需补处理):

  Phase A  元数据回填: 前端可见 + org/industry 缺失 → LLM Analysis (仅文件名, ~2s/篇)
  Phase B  深度提取回填: 正文就绪 (content_chars>200) 且未分析 → 单篇 extract
  Phase C  图片型 PDF: 同步逐册 OCR + 深度提取 (慢, 2-5分钟/册, 默认限额 12)

排序均为 report_id DESC (最新优先 —— 前端第一页先变干净).
与常驻服务的轮次/调度幂等共存: 各阶段都按状态过滤, 重复执行安全.

用法:
  python scripts/backfill_report_analysis.py                 # 全部阶段 (OCR 限 12 册)
  python scripts/backfill_report_analysis.py --ocr-limit 0   # 只跑元数据+深度提取
  python scripts/backfill_report_analysis.py --skip-meta     # 只跑深度提取+OCR
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient

logger = get_logger("backfill_report")

# 与列表 API 一致的"前端可见"过滤
VIS = (r"(file_name ~* '\.(pdf|docx?)$' OR "
       "(file_name IS NULL AND file_path IS NULL AND content_chars > 0)) "
       "AND extraction_status <> 'pending_ocr'")


def _log(msg: str):
    print(msg, flush=True)


def phase_meta(limit: int) -> None:
    """Phase A: 元数据回填 (LLM Analysis, 仅文件名)"""
    from tools.finance.report_meta_analysis import analyze_report_meta
    with PgClient() as pg:
        rows = pg.fetch_all(
            f"SELECT report_id, title FROM fin.report_meta WHERE {VIS} AND "
            "(org_name IS NULL OR org_name='' OR industry IS NULL OR industry='') "
            f"ORDER BY report_id DESC LIMIT {int(limit)}")
    _log(f"[meta] 待回填 {len(rows)} 篇")
    ok = 0
    for i, r in enumerate(rows, 1):
        try:
            if analyze_report_meta(r["report_id"]):
                ok += 1
            _log(f"[meta] {i}/{len(rows)} #{r['report_id']} {r['title'][:40]}")
        except Exception as e:
            _log(f"[meta] {i}/{len(rows)} #{r['report_id']} 失败: {e}")
            time.sleep(1)
    _log(f"[meta] 完成: {ok}/{len(rows)}")


def phase_deep(limit: int) -> None:
    """Phase B: 深度提取回填 (正文就绪, 单篇 extract)"""
    from skills.base import SkillContext
    from skills.report.skill import ReportSkill
    with PgClient() as pg:
        rows = pg.fetch_all(
            f"SELECT report_id, title FROM fin.report_meta WHERE {VIS} AND "
            "content_chars > 200 AND (analysis_status IS NULL OR analysis_status "
            f"NOT IN ('done','failed')) ORDER BY report_id DESC LIMIT {int(limit)}")
    _log(f"[deep] 待回填 {len(rows)} 篇")
    skill = ReportSkill()
    ok = fail = 0
    for i, r in enumerate(rows, 1):
        t0 = time.time()
        try:
            ctx = skill(SkillContext(user_input="", params={
                "mode": "extract", "report_id": r["report_id"], "limit": 1}))
            if ctx.error:
                fail += 1
                _log(f"[deep] {i}/{len(rows)} #{r['report_id']} 失败: {ctx.error}")
            else:
                ok += 1
                _log(f"[deep] {i}/{len(rows)} #{r['report_id']} "
                     f"完成 ({time.time() - t0:.0f}s) {r['title'][:36]}")
        except Exception as e:
            fail += 1
            _log(f"[deep] {i}/{len(rows)} #{r['report_id']} 异常: {e}")
    _log(f"[deep] 完成: ok={ok} fail={fail}")


def phase_ocr(limit: int) -> None:
    """Phase C: 图片型 PDF 同步 OCR + 深度提取 (串行, 不留后台线程)"""
    from skills.base import SkillContext
    from skills.report.skill import ReportSkill
    from tools.finance.pdf_vision import ocr_pdf
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT report_id, title, file_path FROM fin.report_meta "
            "WHERE file_path IS NOT NULL AND content_chars = 0 "
            "AND extraction_status <> 'pending_ocr' "
            "AND (analysis_status IS NULL OR analysis_status NOT IN ('done','failed')) "
            r"AND file_name ~* '\.pdf$' "
            f"ORDER BY report_id DESC LIMIT {int(limit)}")
    _log(f"[ocr] 待处理 {len(rows)} 册")
    skill = ReportSkill()
    ok = fail = 0
    for i, r in enumerate(rows, 1):
        rid, path = r["report_id"], r["file_path"]
        if not os.path.exists(path):
            with PgClient() as pg:
                pg.execute("UPDATE fin.report_meta SET analysis_status='failed' "
                           "WHERE report_id=%s", (rid,))
            fail += 1
            _log(f"[ocr] {i}/{len(rows)} #{rid} 源文件丢失, 标记失败")
            continue
        t0 = time.time()
        try:
            text = ocr_pdf(path, max_pages=10)
        except Exception as e:
            text = None
            _log(f"[ocr] {i}/{len(rows)} #{rid} OCR 异常: {e}")
        with PgClient() as pg:
            if not text:
                pg.execute("UPDATE fin.report_meta SET extraction_status='failed', "
                           "analysis_status='failed' WHERE report_id=%s", (rid,))
                fail += 1
                _log(f"[ocr] {i}/{len(rows)} #{rid} OCR 无文本, 标记失败")
                continue
            pg.execute("UPDATE fin.report_meta SET content_text=%s, content_chars=%s, "
                       "extraction_status='extracted' WHERE report_id=%s",
                       (text, len(text), rid))
        try:
            ctx = skill(SkillContext(user_input="", params={
                "mode": "extract", "report_id": rid, "limit": 1}))
            if ctx.error:
                fail += 1
                _log(f"[ocr] {i}/{len(rows)} #{rid} 提取失败: {ctx.error}")
                continue
        except Exception as e:
            fail += 1
            _log(f"[ocr] {i}/{len(rows)} #{rid} 提取异常: {e}")
            continue
        ok += 1
        _log(f"[ocr] {i}/{len(rows)} #{rid} OCR+提取完成 "
             f"({time.time() - t0:.0f}s, {len(text)} 字)")
    _log(f"[ocr] 完成: ok={ok} fail={fail}")


def main():
    parser = argparse.ArgumentParser(description="研报存量积压回填")
    parser.add_argument("--meta-limit", type=int, default=500)
    parser.add_argument("--deep-limit", type=int, default=500)
    parser.add_argument("--ocr-limit", type=int, default=12,
                        help="图片型 PDF 处理册数 (0=跳过 OCR)")
    parser.add_argument("--skip-meta", action="store_true")
    parser.add_argument("--only-meta", action="store_true")
    args = parser.parse_args()

    if not get_config().openai_api_key:
        _log("❌ 未配置 OPENAI_API_KEY")
        sys.exit(1)

    t0 = time.time()
    if not args.skip_meta:
        phase_meta(args.meta_limit)
    if not args.only_meta:
        phase_deep(args.deep_limit)
        if args.ocr_limit > 0:
            phase_ocr(args.ocr_limit)
    _log(f"✅ 回填结束, 总耗时 {(time.time() - t0) / 60:.1f} 分钟")


if __name__ == "__main__":
    main()
