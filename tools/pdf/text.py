# -*- encoding: utf-8 -*-
"""PDF text-layer extraction + magic-number check"""
from __future__ import annotations

from core.logger import get_logger

logger = get_logger(__name__)


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
    try:
        import fitz
        with fitz.open(path) as doc:
            return "\n".join(p.get_text() for p in doc)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 corrupted files fall through the chain
        logger.warning(f"fitz 提取失败, 降级 pdfplumber: {e}")

    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pdfplumber 提取失败, 降级 pypdf: {e}")

    try:
        from pypdf import PdfReader
        with open(path, "rb") as f:
            reader = PdfReader(f)
            return "\n".join((p.extract_text() or "")
                             for p in reader.pages)
    except ImportError:
        logger.warning("PDF 提取库均不可用 (需 pymupdf/pdfplumber/pypdf 之一)")
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PDF 文本提取全部失败 {path}: {e}")
        return ""
