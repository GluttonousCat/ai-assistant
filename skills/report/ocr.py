"""Queue OCR and resume extraction once report text becomes available."""

import threading
from typing import Any, Dict
from core.logger import get_logger
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
    import threading

    rid, fname = (row["report_id"], row.get("file_name") or path)
    report_text.mark_ocr_pending(rid)

    def _job():
        from tools.pdf import ocr_pdf
        from harness.llm import get_vision_llm
        from skills.report.prompts import VISION_OCR_PROMPT

        try:
            with _OCR_SEMAPHORE:
                text = ocr_pdf(
                    path,
                    prompt=VISION_OCR_PROMPT,
                    max_pages=10,
                    vision_llm=get_vision_llm(),
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
            if not ok:
                return
            from skills.base import SkillContext
            from skills.report.skill import ReportSkill

            ctx = ReportSkill()(
                SkillContext(
                    user_input="",
                    params={"mode": "extract", "report_id": rid, "limit": 1},
                )
            )
            if ctx.error:
                logger.warning(f"OCR 后自动提取失败 #{rid}: {ctx.error}")
            else:
                logger.info(f"OCR 后自动提取完成 #{rid}")
        except Exception as e:
            report_text.mark_ocr_failed(rid)
            logger.warning(f"视觉提取失败 #{rid}: {e}")

    threading.Thread(target=_job, daemon=True, name=f"ocr-{rid}").start()
    logger.info(f"图片型 PDF 转后台视觉识别: {fname}")
    return False
