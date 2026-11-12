# -*- encoding: utf-8 -*-
"""PDF page rendering and image-based-PDF detection"""
from __future__ import annotations

import base64
from typing import List, Tuple

from core.logger import get_logger

logger = get_logger(__name__)


def is_image_pdf(path: str, max_pages_check: int = 3) -> bool:
    """Detect whether a PDF is image-based/scanned (first few pages have an
    empty text layer plus at least one image per page).

    This check is the OCR gate: if the text layer has content, go straight
    to extract_text and save the vision-call cost. Any exception or missing
    library returns False (prefer one wasted empty extraction over
    misclassifying a normal PDF as scanned).
    """
    try:
        import fitz
    except ImportError:
        return False
    try:
        with fitz.open(path) as doc:
            n = min(len(doc), max_pages_check)
            if n == 0:
                return False
            for i in range(n):
                page = doc[i]
                if page.get_text().strip():
                    return False
                if not page.get_images():
                    return False
            return True
    except Exception:
        return False


def render_pages_png(path: str, max_pages: int = 10,
                     dpi: int = 130) -> List[Tuple[int, str]]:
    """Render pages as base64 PNG. Returns [(1-based page no., b64)]; returns
    [] when the file cannot be opened or a library is missing.

    Empirical dpi: 130 is sufficient for report body-text sizes and keeps
    token cost down; raise to 200 for scanned documents.
    """
    try:
        import fitz
    except ImportError:
        logger.warning("render_pages_png 需要 PyMuPDF (fitz)")
        return []
    out: List[Tuple[int, str]] = []
    try:
        with fitz.open(path) as doc:
            for i in range(min(len(doc), max_pages)):
                pix = doc[i].get_pixmap(dpi=dpi)
                b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
                out.append((i + 1, b64))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PDF 页渲染失败 {path}: {e}")
    return out
