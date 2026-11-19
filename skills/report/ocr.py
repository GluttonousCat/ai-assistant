"""Queue OCR and resume extraction once report text becomes available."""

import threading
from typing import Any, Dict
from core.logger import get_logger
from core.observability import observed, task_progress
from tools.finance import report_text

logger = get_logger(__name__)
_OCR_SEMAPHORE = threading.BoundedSemaphore(4)


def queue_ocr(row: Dict[str, Any]) -> bool:
    """
    Vision OCR backfill for image-type PDFs: when a local file exists but
    content_chars=0, render pages -> recognize via the vision model ->
    write content_text back.
    Returns whether text was successfully obtained.
    """
    path = row.get("file_path")
    if not path or row.get("content_chars"):
        return bool(row.get("content_chars"))
    if row.get("extraction_status") == "pending_ocr":
        return False

    rid, fname = (row["report_id"], row.get("file_name") or path)
    if report_text.mark_ocr_pending(rid) == 0:
        return False

    @observed("report_ocr", "研报视觉OCR与接续提取")
    def _job():
        from tools.pdf import ocr_pdf
        from harness.llm import get_vision_llm
        from skills.report.prompts import VISION_OCR_PROMPT

        try:
            task_progress(f"研报 #{rid} 等待OCR工作槽")
            with _OCR_SEMAPHORE:
                task_progress(f"研报 #{rid} 正在进行视觉OCR")
                text = ocr_pdf(
                    path,
                    prompt=VISION_OCR_PROMPT,
                    max_pages=10,
                    vision_llm=get_vision_llm(),
                    page_markers=True,
                )
            ok = False
            if text:
                n = report_text.save_ocr_text(rid, text)
                if n == 0:
                    logger.info(f"#{rid} OCR 完成但记录已被去重删除, 结果丢弃")
                else:
                    logger.info(f"图片型 PDF 视觉识别完成 #{rid}: {len(text)} 字")
                    ok = True
            else:
                report_text.mark_ocr_failed(rid)
                task_progress(warning="OCR未返回可用正文")
            if not ok:
                return
            from skills.base import SkillContext
            from skills.report.skill import ReportSkill

            task_progress(f"研报 #{rid} OCR完成，正在接续深度提取")
            ctx = ReportSkill()(
                SkillContext(
                    user_input="",
                    params={"mode": "extract", "report_id": rid, "limit": 1},
                )
            )
            if ctx.error or (ctx.result or {}).get("failed"):
                error = ctx.error or "未生成可用提取结果"
                task_progress(warning=error)
                logger.warning(f"OCR 后自动提取失败 #{rid}: {error}")
            else:
                logger.info(f"OCR 后自动提取完成 #{rid}")
        except Exception as e:
            task_progress(warning=f"OCR或接续提取失败: {e}")
            report_text.mark_ocr_failed(rid)
            logger.warning(f"视觉提取失败 #{rid}: {e}")

    threading.Thread(target=_job, daemon=True, name=f"ocr-{rid}").start()
    logger.info(f"图片型 PDF 转后台视觉识别: {fname}")
    return False
