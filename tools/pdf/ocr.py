# -*- encoding: utf-8 -*-
"""PDF 视觉 OCR (渲染 PNG → 多模态 LLM 逐页识别)

模型走 purpose="vision" 路由 (当前 qwen3.8-flash, config.yaml llm.models)。
prompt 必填: 版式知识是领域的事 (金融研报/年报/合同各不相同),
由调用方从自己的 prompts.py 注入 —— 本层只是通用识别引擎。
"""
from __future__ import annotations

from typing import List, Optional

from core.logger import get_logger
from tools.pdf.render import render_pages_png

logger = get_logger(__name__)


def ocr_page_png(b64_png: str, prompt: str) -> str:
    """单页视觉 OCR。prompt 为该页的版式要求 (由领域层提供)。"""
    from core.llm.client import get_vision_llm
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
            {"type": "text", "text": "提取这一页的全部内容。"},
        ]},
    ]
    return get_vision_llm().invoke(messages)


def ocr_pdf(path: str, prompt: str, max_pages: int = 10,
            dpi: int = 130) -> Optional[str]:
    """扫描件 PDF 整册识别: 逐页渲染 → OCR → 拼接。

    成本提示: 每页一次视觉调用 (max_pages 控制上限)。
    返回全文; 全部页面失败或结果过短 (≤50 字) 返回 None。
    """
    from core.config import get_config
    if not get_config().openai_api_key:
        return None

    pages: List[str] = []
    rendered = render_pages_png(path, max_pages=max_pages, dpi=dpi)
    for page_no, b64 in rendered:
        try:
            text = ocr_page_png(b64, prompt)
            if text and text.strip():
                pages.append(text.strip())
                logger.info(f"OCR 第 {page_no}/{len(rendered)} 页识别完成 "
                            f"({len(text)} 字)")
        except Exception as e:  # noqa: BLE001 单页失败不弃整册
            logger.warning(f"OCR 第 {page_no} 页识别失败: {e}")

    if not pages:
        return None
    result = "\n".join(pages)
    return result if len(result) > 50 else None
