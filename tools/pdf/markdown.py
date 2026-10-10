# -*- encoding: utf-8 -*-
"""PDF to Markdown"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Union

import pymupdf4llm

from core.logger import get_logger

logger = get_logger(__name__)


def to_markdown(path: str,
                output_md_path: Optional[str] = None,
                pages: Optional[Union[List[int], range]] = None,
                ignore_header_footer: bool = True,
                save_as_chunks: bool = False) -> Union[str, List[dict]]:

    pdf_file = Path(path)
    if not pdf_file.exists():
        raise FileNotFoundError(f"文件不存在: {path}")

    margins = (45, 45, 0, 0) if ignore_header_footer else (0, 0, 0, 0)
    page_list = list(pages) if pages is not None else None

    try:
        result: Union[str, List[dict]]
        if save_as_chunks:
            result = pymupdf4llm.to_markdown(
                doc=str(pdf_file), pages=page_list, margins=margins,
                page_chunks=True, write_images=False)
        else:
            result = pymupdf4llm.to_markdown(
                doc=str(pdf_file), pages=page_list, margins=margins,
                write_images=False)

        if (isinstance(result, str) and len(result.strip()) > 100) or result:
            if output_md_path:
                _write(Path(output_md_path), result)
            return result
        logger.warning(f"MD 转换结果过短, 走文本降级: {path}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MD 转换失败降级纯文本 {path}: {e}")

    if save_as_chunks:
        return []
    # 降级: 纯文本层 + 分页标记 (保章节可定位性)
    from tools.pdf.text import extract_text
    text = extract_text(path)
    if not text:
        return ""
    out, n = [], 0
    for i, line in enumerate(text.splitlines()):
        if i and i % 40 == 0:
            n += 1
            out.append(f"\n\n----- [第{n}页段] -----\n\n")
        out.append(line)
    md = "".join(out)
    if output_md_path:
        _write(Path(output_md_path), md)
    return md


def _write(dest: Path, data) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, list):
        dest = dest.with_suffix(".json")
        dest.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        dest.write_text(data, encoding="utf-8")
    logger.info(f"已保存: {dest}")
