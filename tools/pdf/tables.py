# -*- encoding: utf-8 -*-
"""PDF 表格提取 (pdfplumber, 线框表/准线框表)

适用: 定期报告里的财务表/股东表等结构化线框表格。
"""
from __future__ import annotations

from typing import Dict, List

from core.logger import get_logger

logger = get_logger(__name__)


def extract_tables(
        path: str, max_pages: int = 20,min_rows: int = 2
) -> List[Dict]:
    try:
        import pdfplumber
    except ImportError:
        logger.warning("extract_tables 需要 pdfplumber")
        return []
    out: List[Dict] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages], 1):
                for t in page.extract_tables():
                    rows = [[(c or "").strip() for c in row] for row in t]
                    if len(rows) >= min_rows:
                        out.append({"page": i, "rows": rows})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"表格提取失败 {path}: {e}")
    return out
