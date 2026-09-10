# -*- encoding: utf-8 -*-
"""
ScannedReportSkill - 扫描件研报分析 Agent

【方案】图片型 PDF (无文本层) 的最优处理路径:
    ① PyMuPDF 渲染每页为 PNG (dpi 130, 兼顾清晰度与 token 成本)
    ② 多模态模型 (qwen3.8-flash) 逐页 OCR — prompt 带金融研报版式知识 (表格/数字保真)
    ③ OCR 全文回写 fin.report_meta.content_text (成为普通文本研报)
    ④ 组合调用 ReportSkill 深度提取 (评级/盈利预测/tags) — 复用而非重写
    ⑤ 可选: 综合解读 (SCANNED_ANALYSIS_PROMPT, 买方研究员视角)

【解耦设计】
    - 视觉能力在 tools/finance/pdf_vision (纯工具, 无业务)
    - 本 skill 只做编排; 深度提取委托 ReportSkill (组合)
    - 专业知识全部在 prompts.py — 调优 prompt 不动代码
    - 常规链路 (ReportSkill) 发现无正文图片 PDF 时也调用同一工具自动回填

用法:
    skill(SkillContext(user_input="", params={"report_id": 289}))
    skill(SkillContext(user_input="分析扫描件研报 289"))
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from agent.skills.base import BaseSkill, SkillContext
from agent.skills.report.skill import ReportSkill
from tools.finance.pdf_vision import is_image_pdf, ocr_pdf

logger = get_logger(__name__)


class ScannedReportSkill(BaseSkill):
    name = "scanned_report"
    description = ("扫描件研报分析: 图片型 PDF 逐页视觉识别(OCR)回写正文, "
                   "再进行结构化提取与专业解读")
    intent_keywords = ["扫描件", "扫描研报", "图片PDF", "图片pdf", "扫描文档"]

    # 视觉 OCR 每册页数上限 (成本护栏; 研报通常 5-30 页)
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

            # 已有正文: 跳过 OCR, 直接走分析
            if (row.get("content_chars") or 0) > 200:
                context.log(f"#{report_id} 已有正文 {row['content_chars']} 字, 跳过 OCR")
                return self._analyze(context, int(report_id),
                                     params.get("with_summary", True))

            path = row.get("file_path")
            if not path or not is_image_pdf(path):
                context.result = {"summary": f"#{report_id} 非图片型 PDF 或文件缺失: {path}"}
                return context

            # ①② 视觉 OCR (专业版式 prompt)
            from agent.skills.scanned_report.prompts import SCANNED_OCR_PROMPT
            context.log(f"#{report_id} 开始视觉识别 (最多 {self.MAX_OCR_PAGES} 页)…")
            text = ocr_pdf(path, max_pages=self.MAX_OCR_PAGES,
                           prompt=SCANNED_OCR_PROMPT)
            if not text:
                context.error = f"#{report_id} 视觉识别失败 (全部页面无有效内容)"
                return context

            # ③ 回写正文 — 从此与普通文本研报同构
            with PgClient() as pg:
                pg.execute(
                    "UPDATE fin.report_meta SET content_text=%s, content_chars=%s, "
                    "extraction_status='extracted' WHERE report_id=%s",
                    (text, len(text), report_id))
            context.log(f"#{report_id} OCR 完成 {len(text)} 字, 已回写正文")

            # ④⑤ 深度提取 + 综合解读 (组合 ReportSkill)
            return self._analyze(context, report_id,
                                 params.get("with_summary", True))
        except Exception as e:
            logger.exception("ScannedReportSkill 执行失败")
            context.error = f"扫描件分析失败: {e}"
        return context

    # ---------- 内部 ----------
    def _analyze(self, context: SkillContext, report_id: int,
                 with_summary: bool) -> SkillContext:
        """④ 深度提取 (委托 ReportSkill, 幂等) ⑤ 可选综合解读"""
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

        # 综合解读 (agent 模型)
        from core.llm.client import get_agent_llm
        from agent.skills.scanned_report.prompts import SCANNED_ANALYSIS_PROMPT
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
