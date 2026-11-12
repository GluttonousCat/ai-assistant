# -*- encoding: utf-8 -*-
"""
Generic PDF tool layer (domain-agnostic primitives).

Capabilities (see each module for full signatures):
    extract_text(path)            full text layer (fitz → pdfplumber → pypdf fallback chain)
    is_pdf_file(path)             %PDF magic-number check
    is_image_pdf(path)            image-based/scanned detection (empty text layer + image per page)
    render_pages_png(path, ...)   render pages as base64 PNG
    ocr_pdf(path, prompt, vision_llm, ...)   whole-document vision OCR (prompt/vision client are required)
    ocr_page_png(b64, prompt, vision_llm)    single-page vision OCR
    extract_tables(path, ...)     pdfplumber table extraction

Boundary convention: no domain knowledge ever enters this layer —
    - financial research-report layout prompts → skills/report/prompts.py (VISION_OCR_PROMPT)
    - the vision model client is injected by the caller (vision_llm param,
      e.g. harness.llm.get_vision_llm()); this layer must not import
      harness.llm
    - broker-report noise cleaning / multi-format dispatch → tools/finance/report_extractor.py
    - annual-report section slicing → skills/content/annual_report.py
"""
from tools.pdf.text import extract_text, is_pdf_file
from tools.pdf.render import is_image_pdf, render_pages_png
from tools.pdf.ocr import ocr_page_png, ocr_pdf
from tools.pdf.tables import extract_tables
from tools.pdf.markdown import to_markdown

__all__ = ["extract_text", "is_pdf_file", "is_image_pdf",
           "render_pages_png", "ocr_page_png", "ocr_pdf", "extract_tables"]
