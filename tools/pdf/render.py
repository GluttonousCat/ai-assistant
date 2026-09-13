# -*- encoding: utf-8 -*-
"""PDF 页渲染与图片型判定"""
from __future__ import annotations

import base64
from typing import List, Tuple

from core.logger import get_logger

logger = get_logger(__name__)


def is_image_pdf(path: str, max_pages_check: int = 3) -> bool:
    """判断是否为图片型/扫描件 PDF (前几页零文本层 + 每页含大图)。

    判定即 OCR 闸门: 文本层有内容直接走 extract_text, 省视觉调用成本。
    任何异常/缺库返回 False (宁可多一次空提取, 不误杀正常 PDF)。
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
    """页渲染为 base64 PNG。返回 [(页码1based, b64)]; 打不开/缺库返回 []。

    dpi 经验值: 130 对研报正文字号足够且控制 token 成本, 扫描件可提至 200。
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
