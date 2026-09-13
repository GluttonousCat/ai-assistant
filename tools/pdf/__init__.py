# -*- encoding: utf-8 -*-
"""
PDF 通用工具层 (与领域无关的原语集)

能力 (详细签名见各模块):
    extract_text(path)            文本层全文 (fitz→pdfplumber→pypdf 降级链)
    is_pdf_file(path)             %PDF 魔数校验
    is_image_pdf(path)            图片型/扫描件判定 (零文本层+每页大图)
    render_pages_png(path, ...)   页渲染 base64 PNG
    ocr_pdf(path, prompt, ...)    视觉 OCR 整册识别 (prompt 必填)
    ocr_page_png(b64, prompt)     单页视觉 OCR
    extract_tables(path, ...)     pdfplumber 表格提取

边界约定: 领域知识一律不进本层 ——
    - 金融研报版式 prompt → skills/report/prompts.py (VISION_OCR_PROMPT)
    - 券商研报噪音清洗/多格式分派 → tools/finance/report_extractor.py
    - 年报章节切片 → skills/content/annual_report.py
"""
from tools.pdf.text import extract_text, is_pdf_file
from tools.pdf.render import is_image_pdf, render_pages_png
from tools.pdf.ocr import ocr_page_png, ocr_pdf
from tools.pdf.tables import extract_tables
from tools.pdf.markdown import to_markdown

__all__ = ["extract_text", "is_pdf_file", "is_image_pdf",
           "render_pages_png", "ocr_page_png", "ocr_pdf", "extract_tables"]
