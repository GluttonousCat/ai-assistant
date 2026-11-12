# -*- encoding: utf-8 -*-
"""PDF table extraction (pdfplumber; ruled / semi-ruled tables)

Suited to structured ruled tables in periodic reports, e.g. financial
statement and shareholder tables.
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
