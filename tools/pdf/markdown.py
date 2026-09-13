# -*- encoding: utf-8 -*-
"""
PDF To Markdown
"""
from __future__ import annotations

from core.logger import get_logger

logger = get_logger(__name__)


def to_markdown(path: str, max_pages: int = 0) -> str:
    """PDF 全文转 Markdown。"""
    try:
        import pymupdf4llm
        kwargs = {"page_chunks": False}
        if max_pages:
            import fitz
            with fitz.open(path) as doc:
                pages = list(range(min(len(doc), max_pages)))
            md = pymupdf4llm.to_markdown(path, pages=pages, **kwargs)
        else:
            md = pymupdf4llm.to_markdown(path, **kwargs)
        if md and len(md.strip()) > 100:
            return md
        logger.warning(f"MD 转换结果过短 ({len(md or '')} 字), 走文本降级: {path}")
    except ImportError:
        logger.warning("to_markdown 需要 pymupdf4llm, 降级纯文本")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MD 转换失败降级纯文本 {path}: {e}")

    from tools.pdf.text import extract_text
    text = extract_text(path)
    if not text:
        return ""
    pages = text.split("\n")
    out, n = [], 0
    for i, line in enumerate(pages):
        if i and i % 40 == 0:
            n += 1
            out.append(f"\n\n----- [第{n}页段] -----\n\n")
        out.append(line)
    return "".join(out)
