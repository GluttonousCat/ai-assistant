# -*- encoding: utf-8 -*-
"""tools/pdf 单测: fitz 合成 fixture (无外部文件依赖, 不调 LLM)"""
from __future__ import annotations

import pytest

fitz = pytest.importorskip("fitz")

from tools.pdf import (extract_text, extract_tables, is_image_pdf,
                       is_pdf_file, render_pages_png)


# ---------- fixture 工厂 ----------

def _make_text_pdf(path, pages=2):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 96), f"第{i + 1}页: 管理层讨论与分析测试文本 ALPHA-{i}", fontname="china-s")
    doc.save(str(path))
    doc.close()


def _make_image_pdf(path, pages=2):
    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 80))
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)   # 只插图不写字
    doc.save(str(path))
    doc.close()


def _make_table_pdf(path):
    doc = fitz.open()
    page = doc.new_page()
    # 3x3 线框表: 画网格线 + 填单元格文字 (pdfplumber lines 策略可识别)
    x0, y0, cw, rh = 72, 72, 100, 20
    for r in range(4):
        page.draw_line((x0, y0 + r * rh), (x0 + 3 * cw, y0 + r * rh))
    for c in range(4):
        page.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + 3 * rh))
    for r in range(3):
        for c in range(3):
            page.insert_text((x0 + c * cw + 4, y0 + r * rh + 14),
                             f"单元格{r}{c}", fontname="china-s")
    doc.save(str(path))
    doc.close()


# ---------- text ----------

def test_extract_text_text_layer(tmp_path):
    p = tmp_path / "t.pdf"
    _make_text_pdf(p)
    text = extract_text(str(p))
    assert "管理层讨论与分析测试文本" in text
    assert "ALPHA-1" in text          # 多页拼接


def test_is_pdf_file(tmp_path):
    p = tmp_path / "t.pdf"
    _make_text_pdf(p)
    assert is_pdf_file(str(p)) is True
    fake = tmp_path / "fake.pdf"
    fake.write_text("<html>error page</html>")
    assert is_pdf_file(str(fake)) is False
    assert is_pdf_file(str(tmp_path / "nope.pdf")) is False


# ---------- render ----------

def test_is_image_pdf_true(tmp_path):
    p = tmp_path / "img.pdf"
    _make_image_pdf(p)
    assert is_image_pdf(str(p)) is True


def test_is_image_pdf_false_for_text(tmp_path):
    p = tmp_path / "t.pdf"
    _make_text_pdf(p)
    assert is_image_pdf(str(p)) is False


def test_render_pages_png(tmp_path):
    p = tmp_path / "t.pdf"
    _make_text_pdf(p, pages=3)
    pages = render_pages_png(str(p), max_pages=2, dpi=72)
    assert [no for no, _ in pages] == [1, 2]
    assert all(len(b64) > 100 for _, b64 in pages)


# ---------- tables ----------

def test_extract_tables(tmp_path):
    p = tmp_path / "tab.pdf"
    _make_table_pdf(p)
    tables = extract_tables(str(p))
    assert len(tables) >= 1
    assert tables[0]["page"] == 1
    assert len(tables[0]["rows"]) >= 3      # 3 行数据 (可能含表头行)
    flat = "".join("".join(r) for r in tables[0]["rows"])
    assert "单元格" in flat


def test_extract_tables_text_pdf_empty(tmp_path):
    p = tmp_path / "t.pdf"
    _make_text_pdf(p)
    assert extract_tables(str(p)) == []
