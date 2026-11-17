from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skills.report import ocr


def test_existing_text_and_pending_ocr_do_not_enqueue_duplicate_work(monkeypatch):
    thread = MagicMock()
    monkeypatch.setattr(ocr.threading, "Thread", thread)
    assert ocr.queue_ocr({"content_chars": 20}) is True
    assert (
        ocr.queue_ocr({"file_path": "report.pdf", "extraction_status": "pending_ocr"})
        is False
    )
    thread.assert_not_called()


@pytest.mark.parametrize(
    "saved,raises,failed,empty",
    [
        (1, False, False, False),
        (0, False, False, False),
        (1, True, True, False),
        (1, False, True, True),
    ],
)
def test_ocr_write_and_resume_preserve_orphan_and_failure_guards(
    monkeypatch, saved, raises, failed, empty
):
    from skills.report import skill
    import harness.llm
    import tools.pdf

    pending, save, mark_failed = MagicMock(), MagicMock(return_value=saved), MagicMock()
    monkeypatch.setattr(ocr.report_text, "mark_ocr_pending", pending)
    monkeypatch.setattr(ocr.report_text, "save_ocr_text", save)
    monkeypatch.setattr(ocr.report_text, "mark_ocr_failed", mark_failed)
    monkeypatch.setattr(harness.llm, "get_vision_llm", lambda: object())
    text = "" if empty else "正文" * 300
    reader = MagicMock(return_value=text)
    if raises:
        reader.side_effect = RuntimeError("vision failed")
    monkeypatch.setattr(tools.pdf, "ocr_pdf", reader)
    worker = []

    def thread(*, target, **kwargs):
        return SimpleNamespace(start=lambda: worker.append(target))

    monkeypatch.setattr(ocr.threading, "Thread", thread)
    analyze = MagicMock(return_value=SimpleNamespace(error=None))
    monkeypatch.setattr(skill, "ReportSkill", lambda: analyze)
    assert (
        ocr.queue_ocr({"report_id": 7, "file_path": "report.pdf", "content_chars": 0})
        is False
    )
    pending.assert_called_once_with(7)
    assert len(worker) == 1
    worker[0]()
    assert mark_failed.called is failed
    assert analyze.called is (not raises and not empty and saved > 0)
