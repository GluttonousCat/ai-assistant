# -*- encoding: utf-8 -*-
"""
Report text extractor
Extracts local report files (PDF/DOCX/TXT/MD) to plain text for the report
validation/analysis pipeline.

Strategy:
- PDF: prefer PyMuPDF (fitz); fall back to pdfplumber / pypdf when missing
- DOCX: python-docx; degrade to unzipping word/document.xml directly via zipfile
- TXT/MD: read as utf-8 / gbk
"""
from __future__ import annotations


import os
import re
import zipfile
from typing import Dict, Optional
from xml.etree import ElementTree

from core.logger import get_logger
logger = get_logger(__name__)

SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown"}


class ReportExtractor:
    """Report file -> plain text"""

    # Filter common noise lines (headers/footers/page numbers)
    NOISE_PATTERNS = [
        re.compile(r"^\s*[-—–]\s*\d+\s*[-—–]\s*$"),          # page number - 3 -
        re.compile(r"^\s*\d+\s*/\s*\d+\s*$"),                   # 3/12
        re.compile(r"^\s*仅供内部使用.*$", re.I),
        re.compile(r"^\s*免责声明[:：]?.*$"),
        re.compile(r"^\s*\[\s*Table_PageBreak\s*\]\s*$"),
    ]

    def extract(self, file_path: str) -> Optional[str]:
        """Extract file text; returns None on failure"""
        if not os.path.exists(file_path):
            logger.warning("文件不存在: %s", file_path)
            return None

        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".pdf":
                text = self._extract_pdf(file_path)
            elif ext in (".docx", ".doc"):
                text = self._extract_docx(file_path)
            elif ext in (".txt", ".md", ".markdown"):
                text = self._extract_text(file_path)
            else:
                logger.warning("不支持的格式: %s", ext)
                return None
        except Exception as e:
            logger.error("提取失败 %s: %s", file_path, e)
            return None

        if not text:
            return None
        return self.clean(text)

    # ---------- Per-format implementations ----------
    def _extract_pdf(self, path: str) -> str:
        # All PDF primitives go through tools/pdf (fitz -> pdfplumber -> pypdf fallback chain)
        from tools.pdf import extract_text
        return extract_text(path)

    def _extract_docx(self, path: str) -> str:
        # .doc (legacy format) is binary; if zipfile cannot open it, raise and
        # let the caller mark failure
        try:
            import docx  # python-docx

            document = docx.Document(path)
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            return "\n".join(parts)
        except ImportError:
            pass

        # Fallback: read word/document.xml directly via zipfile
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
            texts = [t.text or "" for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")]
            paragraphs.append("".join(texts))
        return "\n".join(paragraphs)

    def _extract_text(self, path: str) -> str:
        for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        return ""

    # ---------- Cleaning ----------
    def clean(self, text: str) -> str:
        """Noise filtering + whitespace compression"""
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(p.match(stripped) for p in self.NOISE_PATTERNS):
                continue
            lines.append(stripped)
        return "\n".join(lines)

    def extract_meta(self, file_path: str) -> Dict:
        """Extract basic metadata (no file read; pure name-based stats)"""
        name = os.path.basename(file_path)
        return {
            "file_name": name,
            "ext": os.path.splitext(name)[1].lower(),
            "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
        }


# ---------- Convenience singleton ----------
_extractor: Optional[ReportExtractor] = None


def get_extractor() -> ReportExtractor:
    global _extractor
    if _extractor is None:
        _extractor = ReportExtractor()
    return _extractor