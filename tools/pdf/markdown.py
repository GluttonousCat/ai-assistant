# -*- encoding: utf-8 -*-
import json
from pathlib import Path
from typing import List, Optional, Union
import pymupdf4llm


def convert_financial_report_to_md(
        pdf_path: str,
        output_md_path: Optional[str] = None,
        pages: Optional[Union[List[int], range]] = None,
        ignore_header_footer: bool = True,
        save_as_chunks: bool = False
) -> Union[str, List[dict]]:
    """
    专门针对 A股财报优化的 PDF 转 Markdown 函数
    :param pdf_path: PDF 财报文件路径
    :param output_md_path: 输出的 MD 文件路径（可选）
    :param pages: 指定转换的页码列表（0 开始计数），例如 range(50, 100)，默认全部
    :param ignore_header_footer: 是否忽略页眉页脚（避免每页重复出现公司名和页码）
    :param save_as_chunks: 是否按页输出为结构化分块（适合大模型 RAG 检索/问答）
    :return: Markdown 字符串 或 结构化分块列表
    """
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        raise FileNotFoundError(f"文件不存在: {pdf_path}")

    margins = (45, 45, 0, 0) if ignore_header_footer else (0, 0, 0, 0)

    print(f"正在解析财报: {pdf_file.name} ...")

    if save_as_chunks:
        chunks = pymupdf4llm.to_markdown(
            doc=str(pdf_file),
            pages=list(pages) if pages is not None else None,
            margins=margins,
            page_chunks=True,
            write_images=False
        )

        json_path = (Path(
            output_md_path) if output_md_path else pdf_file).with_suffix(
            ".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(chunks, f, ensure_ascii=False, indent=2)
        print(f"✅ 结构化分块完成，已保存至: {json_path}")
        return chunks

    else:
        md_text = pymupdf4llm.to_markdown(
            doc=str(pdf_file),
            pages=list(pages) if pages is not None else None,
            margins=margins,
            write_images=False
        )

        if output_md_path is None:
            output_md_path = pdf_file.with_suffix(".md")

        with open(output_md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        print(f"✅ 转换完成，已保存至: {output_md_path}")
        return md_text


def to_markdown(path: str, max_pages: int = 0) -> str:
    """管线适配入口 (annual_core/sec2/annual_sections 调用): PDF → MD 字符串。

    与 convert_financial_report_to_md 的分工: 那个是独立工具 (写文件/分块模式),
    本函数无副作用 — 落盘由调用方按 L0 约定 (output/cninfo/md/) 管理。
    页眉页脚抑制 (margins) 沿用其财报优化; 降级链: pymupdf4llm 失败 →
    纯文本层 + 分页标记; 全败返回空串。
    """
    from core.logger import get_logger
    logger = get_logger(__name__)
    try:
        pages = list(range(max_pages)) if max_pages else None
        md = pymupdf4llm.to_markdown(doc=path, pages=pages,
                                     margins=(45, 45, 0, 0),
                                     write_images=False)
        if md and len(md.strip()) > 100:
            return md
        logger.warning(f"MD 转换结果过短 ({len(md or '')} 字), 走文本降级: {path}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MD 转换失败降级纯文本 {path}: {e}")

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
    return "".join(out)
