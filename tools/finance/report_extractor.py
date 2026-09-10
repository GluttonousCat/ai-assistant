# -*- encoding: utf-8 -*-
"""
研报文本提取器
将本地研报文件 (PDF/DOCX/TXT/MD) 提取为纯文本, 供研报校验/分析链路使用.

策略:
- PDF: 优先 PyMuPDF(fitz), 缺失时尝试 pdfplumber / pypdf
- DOCX: python-docx, 缺失时退化为 zipfile 直接解 word/document.xml
- TXT/MD: utf-8 / gbk 读入
"""
from __future__ import annotations

import logging
import os
import re
import zipfile
from typing import Dict, Optional
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown"}


class ReportExtractor:
    """研报文件 → 纯文本"""

    # 常见噪音行 (页眉页脚/页码) 过滤
    NOISE_PATTERNS = [
        re.compile(r"^\s*[-—–]\s*\d+\s*[-—–]\s*$"),          # 页码 - 3 -
        re.compile(r"^\s*\d+\s*/\s*\d+\s*$"),                   # 3/12
        re.compile(r"^\s*仅供内部使用.*$", re.I),
        re.compile(r"^\s*免责声明[:：]?.*$"),
        re.compile(r"^\s*\[\s*Table_PageBreak\s*\]\s*$"),
    ]

    def extract(self, file_path: str) -> Optional[str]:
        """提取文件文本, 失败返回 None"""
        if not os.path.exists(file_path):
            logger.warning("文件不存在: %s", file_path)
            return None

        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".pdf":
                text = self._extract_pdf(file_path)
            elif ext in (".docx", ".doc"):
                text = self._extract_docx(file_path)
            elif ext in (".txt", ".md", ".markdown"):
                text = self._extract_text(file_path)
            else:
                logger.warning("不支持的格式: %s", ext)
                return None
        except Exception as e:
            logger.error("提取失败 %s: %s", file_path, e)
            return None

        if not text:
            return None
        return self.clean(text)

    # ---------- 各格式实现 ----------
    def _extract_pdf(self, path: str) -> str:
        try:
            import fitz  # PyMuPDF

            parts = []
            with fitz.open(path) as doc:
                for page in doc:
                    parts.append(page.get_text())
            return "\n".join(parts)
        except ImportError:
            pass

        # 退化方案
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception:
            pass

        try:
            import pypdf

            with open(path, "rb") as f:
                reader = pypdf.PdfReader(f)
                return "\n".join(p.extract_text() or "" for p in reader.pages)
        except Exception:
            logger.error("pdf 提取库均不可用 (fitz/pdfplumber/pypdf)")
            return ""

    # ---------- 图片型 PDF: 视觉模型 OCR ----------
    def is_image_pdf(self, path: str, max_pages_check: int = 3) -> bool:
        """判断是否为图片型 PDF (前几页零文本层 + 每页有大图)"""
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

    def extract_pdf_visual(self, path: str, max_pages: int = 10,
                           dpi: int = 130) -> Optional[str]:
        """
        图片型 PDF 识别: 页面渲染为 PNG -> 视觉模型逐页提取文字.
        仅在普通文本提取为空且 is_image_pdf 为真时调用 (成本高).
        返回拼接文本; 无视觉模型配置或失败返回 None.
        """
        try:
            import fitz
            import base64
        except ImportError:
            logger.warning("视觉提取需要 PyMuPDF (fitz)")
            return None

        from core.config import get_config
        if not get_config().openai_api_key:
            return None

        from core.llm.client import get_vision_llm
        llm = get_vision_llm()

        pages_text: list = []
        with fitz.open(path) as doc:
            n = min(len(doc), max_pages)
            for i in range(n):
                pix = doc[i].get_pixmap(dpi=dpi)
                b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
                try:
                    text = llm.invoke([
                        {"role": "system", "content":
                            "你是 OCR 引擎。逐字提取图片中的全部文字内容 (研报正文、表格数据), "
                            "保持原始顺序, 不要总结、不要评论、不要添加解释。表格转为可读文本行。"},
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{b64}"}},
                            {"type": "text", "text": "提取这页的全部文字。"},
                        ]},
                    ])
                    if text and text.strip():
                        pages_text.append(text.strip())
                except Exception as e:
                    logger.warning("视觉提取第 %d 页失败: %s", i + 1, e)

        if not pages_text:
            return None
        result = "\n".join(pages_text)
        return result if len(result) > 50 else None

    def _extract_docx(self, path: str) -> str:
        # .doc (老格式) 为二进制, zipfile 解不出时抛出, 由调用方标记失败
        try:
            import docx  # python-docx

            document = docx.Document(path)
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            return "\n".join(parts)
        except ImportError:
            pass

        # 退化: zipfile 直接读 document.xml
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
            texts = [t.text or "" for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")]
            paragraphs.append("".join(texts))
        return "\n".join(paragraphs)

    def _extract_text(self, path: str) -> str:
        for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        return ""

    # ---------- 清洗 ----------
    def clean(self, text: str) -> str:
        """噪音过滤 + 压缩空白"""
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(p.match(stripped) for p in self.NOISE_PATTERNS):
                continue
            lines.append(stripped)
        return "\n".join(lines)

    def extract_meta(self, file_path: str) -> Dict:
        """提取基础元信息 (无文件读取, 纯命名统计)"""
        name = os.path.basename(file_path)
        return {
            "file_name": name,
            "ext": os.path.splitext(name)[1].lower(),
            "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
        }


# ---------- 便捷单例 ----------
_extractor: Optional[ReportExtractor] = None


def get_extractor() -> ReportExtractor:
    global _extractor
    if _extractor is None:
        _extractor = ReportExtractor()
    return _extractor