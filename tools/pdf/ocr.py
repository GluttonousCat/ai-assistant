# -*- encoding: utf-8 -*-
"""PDF OCR"""
from __future__ import annotations

from typing import Any, List, Optional

from core.logger import get_logger
from tools.pdf.render import render_pages_png

logger = get_logger(__name__)


def ocr_page_png(b64_png: str, prompt: str, vision_llm: Any) -> str:
    """单页视觉 OCR"""
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
            {"type": "text", "text": "提取这一页的全部内容。"},
        ]},
    ]
    return vision_llm.invoke(messages)


def ocr_pdf(path: str, prompt: str, vision_llm: Any, max_pages: int = 10,
            dpi: int = 130) -> Optional[str]:
    """扫描件 PDF 整册识别: 逐页渲染 → OCR → 拼接。"""
    from core.config import get_config
    if not get_config().openai_api_key:
        return None

    pages: List[str] = []
    rendered = render_pages_png(path, max_pages=max_pages, dpi=dpi)
    for page_no, b64 in rendered:
        try:
            text = ocr_page_png(b64, prompt, vision_llm)
            if text and text.strip():
                pages.append(text.strip())
                logger.info(f"OCR 第 {page_no}/{len(rendered)} 页识别完成 "
                            f"({len(text)} 字)")
        except Exception as e:  # noqa: BLE001 单页失败不弃整册
            logger.warning(f"OCR 第 {page_no} 页识别失败: {e}")

    if not pages:
        return None
    result = "\n".join(pages)
    return result if len(result) > 10 else None
