# -*- encoding: utf-8 -*-
"""
ScannedReportSkill - scanned report analysis agent

[Approach] optimal path for image-type PDFs (no text layer):
    (1) PyMuPDF renders each page to PNG (dpi 130, balancing clarity and token cost)
    (2) multimodal model (qwen3.8-flash) does page-by-page OCR — the prompt carries
        financial report layout knowledge (table/number fidelity)
    (3) the full OCR text is written back to fin.report_meta.content_text
        (becomes an ordinary text report)
    (4) composed call into ReportSkill deep extraction (rating/forecasts/tags) —
        reuse instead of rewrite
    (5) optional: synthesized interpretation (SCANNED_ANALYSIS_PROMPT, buy-side
        researcher perspective)

[Decoupled design]
    - vision capability lives in tools/pdf (generic PDF tool layer); the vision
      model client is injected by this skill (ocr_pdf/ocr_page_png vision_llm
      parameter), the tools layer knows nothing about harness.llm
    - this skill only orchestrates; deep extraction is delegated to ReportSkill (composition)
    - all domain knowledge lives in prompts.py — tune prompts without touching code
    - the regular pipeline (ReportSkill), upon finding an image PDF with no body
      text, calls the same tool to backfill automatically

Usage:
    skill(SkillContext(user_input="", params={"report_id": 289}))
    skill(SkillContext(user_input="分析扫描件研报 289"))
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from skills.base import BaseSkill, SkillContext
from skills.report.skill import ReportSkill
from tools.pdf import is_image_pdf, ocr_pdf

logger = get_logger(__name__)


class ScannedReportSkill(BaseSkill):
    name = "scanned_report"
    description = ("扫描件研报分析: 图片型 PDF 逐页视觉识别(OCR)回写正文, "
                   "再进行结构化提取与专业解读")
    intent_keywords = ["扫描件", "扫描研报", "图片PDF", "图片pdf", "扫描文档"]

    # Page cap for vision OCR per report (cost guardrail; reports are usually 5-30 pages)
    MAX_OCR_PAGES = 10

    def run(self, context: SkillContext) -> SkillContext:
        params = context.params or {}
        report_id = params.get("report_id") or self._first_report_id(context.user_input)
        if not report_id:
            context.result = {"summary": "请指定研报 report_id, 如: 分析扫描件研报 289"}
            return context
        if not get_config().openai_api_key:
            context.error = "扫描件分析需要 LLM (未配置 OPENAI_API_KEY)"
            return context

        try:
            row = self._load(int(report_id))
            if not row:
                context.result = {"summary": f"研报 #{report_id} 不存在"}
                return context

            # body text already present: skip OCR, go straight to analysis
            if (row.get("content_chars") or 0) > 200:
                context.log(f"#{report_id} 已有正文 {row['content_chars']} 字, 跳过 OCR")
                return self._analyze(context, int(report_id),
                                     params.get("with_summary", True))

            path = row.get("file_path")
            if not path or not is_image_pdf(path):
                context.result = {"summary": f"#{report_id} 非图片型 PDF 或文件缺失: {path}"}
                return context

            # (1)(2) vision OCR (domain-layout prompt; the vision client is injected
            # by this layer, tools/pdf knows nothing about the LLM)
            from harness.llm import get_vision_llm
            from skills.scanned_report.prompts import SCANNED_OCR_PROMPT
            context.log(f"#{report_id} 开始视觉识别 (最多 {self.MAX_OCR_PAGES} 页)…")
            text = ocr_pdf(path, max_pages=self.MAX_OCR_PAGES,
                           prompt=SCANNED_OCR_PROMPT, vision_llm=get_vision_llm())
            if not text:
                context.error = f"#{report_id} 视觉识别失败 (全部页面无有效内容)"
                return context

            # (3) write the body back — from here on it is isomorphic to an ordinary text report
            with PgClient() as pg:
                pg.execute(
                    "UPDATE fin.report_meta SET content_text=%s, content_chars=%s, "
                    "extraction_status='extracted' WHERE report_id=%s",
                    (text, len(text), report_id))
            context.log(f"#{report_id} OCR 完成 {len(text)} 字, 已回写正文")

            # (4)(5) deep extraction + synthesized interpretation (composing ReportSkill)
            return self._analyze(context, report_id,
                                 params.get("with_summary", True))
        except Exception as e:
            logger.exception("ScannedReportSkill 执行失败")
            context.error = f"扫描件分析失败: {e}"
        return context

    # ---------- Internals ----------
    def _analyze(self, context: SkillContext, report_id: int,
                 with_summary: bool) -> SkillContext:
        """(4) deep extraction (delegated to ReportSkill, idempotent) (5) optional synthesized interpretation"""
        report_skill = ReportSkill()
        extract_ctx = report_skill(SkillContext(
            user_input="", params={"mode": "extract", "report_id": report_id}))
        if extract_ctx.error:
            context.error = extract_ctx.error
            return context
        context.log(f"#{report_id} 深度提取完成")

        if not with_summary:
            context.result = {"summary": f"扫描件 #{report_id} 已完成 OCR + 结构化提取入库",
                              "report_id": report_id}
            return context

        # synthesized interpretation (agent model)
        from harness.llm import get_agent_llm
        from skills.scanned_report.prompts import SCANNED_ANALYSIS_PROMPT
        with PgClient() as pg:
            row = pg.fetch_one(
                "SELECT title, content_text FROM fin.report_meta WHERE report_id=%s",
                (report_id,))
        if not row or not (row.get("content_text") or "").strip():
            context.result = {"summary": f"#{report_id} 无正文可解读"}
            return context
        text = row["content_text"][:12000]
        summary = get_agent_llm().invoke(
            f"{SCANNED_ANALYSIS_PROMPT}\n\n研报标题: {row['title']}\n\nOCR 全文:\n{text}")
        context.result = {"summary": summary, "report_id": report_id}
        return context

    @staticmethod
    def _load(report_id: int) -> Optional[Dict[str, Any]]:
        with PgClient() as pg:
            return pg.fetch_one(
                "SELECT report_id, title, file_path, file_name, content_chars "
                "FROM fin.report_meta WHERE report_id=%s", (report_id,))

    @staticmethod
    def _first_report_id(text: str) -> Optional[int]:
        import re
        m = re.search(r"#?(\d{2,6})", text or "")
        return int(m.group(1)) if m else None
