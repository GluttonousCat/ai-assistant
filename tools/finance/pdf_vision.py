# -*- encoding: utf-8 -*-
"""
PDF 视觉工具层: 扫描件/图片型 PDF → 文本

【定位】纯工具 (tools 层), 不含业务逻辑:
    - is_image_pdf: 判断是否图片型 PDF (无文本层)
    - ocr_pdf:      渲染页面为 PNG → 多模态模型逐页识别 (config: llm.models.vision)

被谁使用 (解耦点):
    - skills/scanned_report: 扫描件研报分析 Agent (OCR 后接结构化提取/解读)
    - skills/report: 常规研报链路发现无正文图片 PDF 时的自动回填

模型: 走 purpose="vision" 路由 (当前 qwen3.8-flash, 见 config.yaml llm.models)。
"""
from __future__ import annotations

import base64
from typing import List, Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)


def is_image_pdf(path: str, max_pages_check: int = 3) -> bool:
    """判断是否为图片型/扫描件 PDF (前几页零文本层 + 每页含大图)"""
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


def _vision_llm():
    """多模态模型客户端 (purpose=vision 路由, 见 llm/client.py)"""
    from core.llm.client import get_vision_llm
    return get_vision_llm()


def ocr_page_png(b64_png: str, prompt: Optional[str] = None) -> str:
    """单页图片 OCR (多模态模型)。prompt 可由上层注入专业版式要求。"""
    llm = _vision_llm()
    messages = [
        {"role": "system", "content": prompt or DEFAULT_OCR_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
            {"type": "text", "text": "提取这一页的全部内容。"},
        ]},
    ]
    return llm.invoke(messages)


# 默认 OCR prompt: 金融研报版式感知 (表格/数字保真)
DEFAULT_OCR_PROMPT = (
    "你是金融研报 OCR 引擎。逐字提取图片中的全部内容, 保持原始顺序:\n"
    "- 正文、标题、要点符号 (■/●) 完整保留\n"
    "- 表格转为可读文本行 (如 '营业收入 | 2025A: 100亿 | 2026E: 120亿')\n"
    "- 数字、百分比、货币单位必须精确, 不要四舍五入或改写\n"
    "- 图表只提取其中可见的文字标签与数值, 图形本身用一行 '[图表: 标题]' 表示\n"
    "- 不要总结、不要翻译、不要添加任何解释"
)


def ocr_pdf(path: str, max_pages: int = 10, dpi: int = 130,
            prompt: Optional[str] = None) -> Optional[str]:
    """
    扫描件 PDF 整册识别: 逐页渲染 PNG → 多模态 OCR → 拼接文本。
    返回全文; 全部页面失败返回 None。成本提示: 每页一次视觉调用。
    """
    try:
        import fitz
    except ImportError:
        logger.warning("pdf_vision 需要 PyMuPDF (fitz)")
        return None
    if not get_config().openai_api_key:
        return None

    pages_text: List[str] = []
    with fitz.open(path) as doc:
        n = min(len(doc), max_pages)
        for i in range(n):
            pix = doc[i].get_pixmap(dpi=dpi)
            b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
            try:
                text = ocr_page_png(b64, prompt)
                if text and text.strip():
                    pages_text.append(text.strip())
                    logger.info(f"pdf_vision 第 {i + 1}/{n} 页识别完成 ({len(text)} 字)")
            except Exception as e:
                logger.warning(f"pdf_vision 第 {i + 1} 页识别失败: {e}")

    if not pages_text:
        return None
    result = "\n".join(pages_text)
    return result if len(result) > 50 else None
