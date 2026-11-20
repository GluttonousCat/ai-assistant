from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.offpeak import OffPeakEnded
from skills.report import ocr


def test_ocr_submissions_recover_pending_records_and_use_same_key(monkeypatch):
    submit = Mock()
    monkeypatch.setattr(ocr, "enqueue", submit)
    assert ocr.queue_ocr({"content_chars": 20}) is True
    submit.assert_not_called()
    for state in ("pending", "pending_ocr"):
        assert not ocr.queue_ocr({"report_id": 7, "file_path": "scan.pdf", "extraction_status": state})
    assert submit.call_count == 2
    submit.assert_called_with("report_ocr", report_id=7)


@pytest.mark.parametrize("outcome", ("saved", "orphan", "empty", "error", "cutoff", "resumed"))
def test_ocr_is_resumable_and_preview_text_is_not_a_source(monkeypatch, tmp_path, outcome):
    path = tmp_path / "scan.pdf"
    path.touch()
    row = {"report_id": 7, "file_path": str(path), "content_chars": 25}
    monkeypatch.setattr(ocr.report_analysis, "load_report", lambda _: row)
    monkeypatch.setattr(ocr, "load_document", lambda _: SimpleNamespace(segments=[1] if outcome == "resumed" else []))
    monkeypatch.setattr("harness.llm.get_vision_llm", lambda: object())
    reader = Mock(return_value="" if outcome == "empty" else "正文" * 50)
    if outcome in ("error", "cutoff"):
        reader.side_effect = OffPeakEnded() if outcome == "cutoff" else RuntimeError("vision failed")
    monkeypatch.setattr("tools.pdf.ocr_pdf", reader)
    save = Mock(return_value=outcome != "orphan")
    failed, analyze = Mock(), Mock(return_value=True)
    monkeypatch.setattr(ocr.report_text, "mark_ocr_pending", Mock())
    monkeypatch.setattr(ocr.report_text, "mark_ocr_failed", failed)
    monkeypatch.setattr(ocr.report_text, "save_ocr_text", save)
    monkeypatch.setattr("skills.report.skill.ReportSkill", lambda: SimpleNamespace(_extract_one=analyze))
    if outcome in ("error", "empty", "cutoff"):
        with pytest.raises(OffPeakEnded if outcome == "cutoff" else RuntimeError):
            ocr.process_ocr.__wrapped__(7)
    else:
        ocr.process_ocr.__wrapped__(7)
    assert reader.called == (outcome != "resumed")
    assert failed.called == (outcome in ("error", "empty"))
    assert analyze.called == (outcome in ("saved", "resumed"))
