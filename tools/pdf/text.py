# -*- encoding: utf-8 -*-
"""PDF text-layer extraction + magic-number check"""
from __future__ import annotations

import threading

from core.logger import get_logger

logger = get_logger(__name__)
_TABLE_LOCK = threading.Lock()


def is_pdf_file(path: str) -> bool:
    """%PDF magic-number check (guards against error pages / HTML saved as
    fake PDFs; used by downloaders for post-save validation)"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


def extract_text(path: str) -> str:
    """Full PDF text-layer extraction. Fallback chain: fitz → pdfplumber →
    pypdf; returns an empty string when all fail.

    Note: image-based/scanned PDFs have an empty text layer (empty string
    returned); route them to ocr_pdf for vision recognition.
    """
    return "\n".join(extract_text_pages(path))


def extract_text_pages(path: str, *, layout: bool = False) -> list[str]:
    """Preserve physical page order, including pages without a text layer."""
    try:
        import fitz
        with fitz.open(path) as doc:
            return [_layout_text(p) if layout else p.get_text() for p in doc]
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 corrupted files fall through the chain
        logger.warning(f"fitz 提取失败, 降级 pdfplumber: {e}")

    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return [p.extract_text() or "" for p in pdf.pages]
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pdfplumber 提取失败, 降级 pypdf: {e}")

    try:
        from pypdf import PdfReader
        with open(path, "rb") as f:
            reader = PdfReader(f)
            return [p.extract_text() or "" for p in reader.pages]
    except ImportError:
        logger.warning("PDF 提取库均不可用 (需 pymupdf/pdfplumber/pypdf 之一)")
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PDF 文本提取全部失败 {path}: {e}")
        return []


def _layout_text(page) -> str:
    text = page.get_text(sort=True)
    if not text.strip():
        return ""
    try:
        rows = []
        # PyMuPDF table detection uses shared layout state.
        with _TABLE_LOCK:
            for table in page.find_tables().tables:
                if table.row_count < 3 or table.col_count < 2:
                    continue
                rows.append("\n".join(
                    " | ".join(" ".join((cell or "").split()) for cell in row)
                    for row in table.extract()
                ))
        if rows:
            text += "\n\n[表格]\n" + "\n\n".join(rows)
    except Exception as e:
        logger.debug("PDF 第 %s 页表格识别失败: %s", page.number + 1, e)
    return text
