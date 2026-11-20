"""Queue OCR and resume extraction once report text becomes available."""

from typing import Any, Dict
from core.logger import get_logger
from core.observability import observed, task_progress
from core.offpeak import OffPeakEnded
from core.offpeak_queue import enqueue
from skills.report.document import load_document
from tools.finance import report_analysis, report_text

logger = get_logger(__name__)


def queue_ocr(row: Dict[str, Any]) -> bool:
    """Register a scanned attachment without starting paid work now."""
    if not row.get("file_path") or row.get("content_chars"):
        return bool(row.get("content_chars"))
    enqueue("report_ocr", report_id=row["report_id"])
    return False


@observed("report_ocr", "闲时研报 OCR 与接续提取")
def process_ocr(report_id):
    from pathlib import Path
    from harness.llm import get_vision_llm
    from skills.report.prompts import VISION_OCR_PROMPT
    from tools.pdf import ocr_pdf

    row = report_analysis.load_report(report_id)
    if not row:
        return
    if not load_document(row).segments:
        path = row.get("file_path") or ""
        if not path or not Path(path).is_file():
            report_text.mark_ocr_failed(report_id)
            report_analysis.mark_failed(row)
            return
        report_text.mark_ocr_pending(report_id)
        task_progress(f"研报 #{report_id} 正在进行视觉 OCR")
        try:
            text = ocr_pdf(path, prompt=VISION_OCR_PROMPT, max_pages=10,
                           vision_llm=get_vision_llm(), page_markers=True)
        except OffPeakEnded:
            raise
        except Exception:
            report_text.mark_ocr_failed(report_id)
            raise
        if not text:
            report_text.mark_ocr_failed(report_id)
            raise RuntimeError("OCR 未返回可用正文")
        if not report_text.save_ocr_text(report_id, text):
            return  # The attachment was deduplicated while OCR was running.

    from skills.report.skill import ReportSkill

    task_progress(f"研报 #{report_id} 接续深度提取")
    current = report_analysis.load_report(report_id)
    if current and not ReportSkill()._extract_one(current):
        logger.warning("研报 #%s OCR 后未保存新的分析结果", report_id)
