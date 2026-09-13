# -*- encoding: utf-8 -*-
"""PDF 文本层提取 + 魔数校验"""
from __future__ import annotations

from core.logger import get_logger

logger = get_logger(__name__)


def is_pdf_file(path: str) -> bool:
    """%PDF 魔数校验 (防错误页/HTML 存成假 PDF; 供下载器等落盘后校验)"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


def extract_text(path: str) -> str:
    """PDF 文本层全文提取。降级链: fitz → pdfplumber → pypdf, 全败返回空串。

    注意: 图片型/扫描件 PDF 文本层为空 (返回空串), 需走 ocr_pdf 视觉识别。
    """
    try:
        import fitz
        with fitz.open(path) as doc:
            return "\n".join(p.get_text() for p in doc)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 损坏文件降级链兜底
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
