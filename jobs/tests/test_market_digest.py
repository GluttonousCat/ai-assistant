from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser
import smtplib

import pytest

from core.local_state import write_json
from jobs import send_market_digest as job
from skills.trend.tests.test_digest import row
from tools.notifications.email import SmtpSettings, DeliveryUncertain


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    snapshot = {"run_id": "run-a", "as_of": "2026-11-16 15:00:00", "version": "same-rules",
                "metadata": {"config": {"fresh_reversal": True}, "timeframes": {
                    p: {"as_of": "2026-11-16 15:00:00"} for p in ("1D", "60F")}},
                "data": [row("600000.SH")]}
    monkeypatch.setattr(job, "_root", lambda: tmp_path)
    monkeypatch.setattr(job, "options", lambda: ("reader@example.com", 60., True))
    monkeypatch.setattr(job.SmtpSettings, "load", lambda: SmtpSettings("smtp.example.com", 465, "sender@example.com", "secret", "sender@example.com"))
    monkeypatch.setattr(job, "load_dashboard", lambda *a, **kw: snapshot)
    monkeypatch.setattr(job, "market_state", lambda: {"status": "complete", "target": "2026-11-16", "snapshot": {"run_id": "run-a"}})
    monkeypatch.setattr(job, "scan_cutoffs", lambda now: {
        "bar_end": datetime(2026, 11, 16, 15), "daily_end": date(2026, 11, 16), "factor_date": date(2026, 11, 16)})
    monkeypatch.setattr(job, "stock_cards", lambda snap, rows: [
        {"row": r, "cid": "stock-"+r["ts_code"].replace(".", "-"), "png": b"png"} for r in rows])
    return snapshot, tmp_path


def test_send_is_once_per_local_day_and_accepts_utc_clock(delivery, monkeypatch):
    calls = []
    monkeypatch.setattr(job, "send", lambda msg, cfg: calls.append(msg))
    utc = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
    assert job.deliver_once(utc)["status"] == "sent"
    assert job.deliver_once(utc)["already_processed"]
    assert len(calls) == 1 and calls[0]["To"] == "reader@example.com"


def test_before_18_does_not_send_or_load_credentials(monkeypatch):
    monkeypatch.setattr(job, "options", lambda: pytest.fail("not due"))
    assert job.deliver_once(datetime(2026, 11, 16, 17, 59))["status"] == "not_due"


def test_explicit_test_send_is_labeled_and_does_not_consume_daily_delivery(delivery, monkeypatch):
    calls = []
    monkeypatch.setattr(job, "send", lambda msg, cfg: calls.append(msg))
    result = job.deliver_once(datetime(2026, 11, 16, 14), test=True)
    assert result["status"] == "sent" and result["test"]
    assert "[测试]" in str(calls[0]["Subject"])
    assert "测试邮件" in calls[0].get_body(preferencelist=("html",)).get_content()
    assert job.deliver_once(datetime(2026, 11, 16, 14), test=True)["already_processed"]
    assert job.deliver_once(datetime(2026, 11, 16, 18))["status"] == "sent"
    assert len(calls) == 2 and calls[0]["Message-ID"] != calls[1]["Message-ID"]


def test_stale_snapshot_waits_without_sending_old_stocks(delivery, monkeypatch):
    snapshot, _ = delivery
    snapshot["as_of"] = "2026-11-13 15:00:00"
    monkeypatch.setattr(job, "send", lambda *a: pytest.fail("stale snapshot must not send"))
    assert job.deliver_once(datetime(2026, 11, 16, 18))["status"] == "waiting_data"


def test_lost_ack_stops_retries_instead_of_sending_twice(delivery, monkeypatch):
    calls = []

    def lost(*args):
        calls.append(True)
        raise DeliveryUncertain()

    monkeypatch.setattr(job, "send", lost)
    assert job.deliver_once(datetime(2026, 11, 16, 18))["status"] == "uncertain"
    assert job.deliver_once(datetime(2026, 11, 16, 19))["status"] == "uncertain"
    assert len(calls) == 1


def test_test_retry_requires_explicit_acknowledgement_and_preserves_history(delivery, monkeypatch):
    _, root = delivery
    attempts = []

    def lost(*args):
        attempts.append(True)
        raise DeliveryUncertain()

    monkeypatch.setattr(job, "send", lost)
    assert job.deliver_once(datetime(2026, 11, 16, 14), test=True)["status"] == "uncertain"
    assert job.deliver_once(datetime(2026, 11, 16, 14), test=True)["status"] == "uncertain"
    monkeypatch.setattr(job, "send", lambda *args: attempts.append(True))
    assert job.deliver_once(datetime(2026, 11, 16, 15), test=True, retry_uncertain_test=True)["status"] == "sent"
    from core.local_state import read_json
    state = read_json(next(root.rglob("delivery.json")), strict=True)
    assert len(state["uncertain_history"]) == 1
    assert state["uncertain_history"][0]["parts"][0]["status"] == "uncertain"
    assert len(attempts) == 2
    with pytest.raises(ValueError):
        job.deliver_once(datetime(2026, 11, 16, 18), retry_uncertain_test=True)


def test_part_retry_resumes_without_resending_accepted_parts(delivery, monkeypatch):
    snapshot, root = delivery
    snapshot["data"] = [row(f"{n:06d}.SH") for n in range(21)]
    # An interrupted batch from the previous multipart version keeps its receipts.
    folder = root / "2026-11-16" / job._profile("reader@example.com", 60., True)
    cards = job.stock_cards(snapshot, snapshot["data"])
    parts = job._prepare(folder / "report", snapshot, cards[:10], date(2026, 11, 16),
                         60., "reader@example.com", "sender@example.com")
    second = job._prepare(root / "legacy-second", snapshot, cards[10:], date(2026, 11, 16),
                          60., "reader@example.com", "sender@example.com")[0]
    msg = BytesParser(policy=policy.default).parsebytes((root / "legacy-second" / second["file"]).read_bytes())
    msg.replace_header("Message-ID", "<legacy-part-2@example.com>")
    (folder / "report" / "part-02.eml").write_bytes(msg.as_bytes(policy=policy.SMTP))
    parts.append({"file": "part-02.eml", "status": "prepared", "message_id": str(msg["Message-ID"])})
    write_json(folder / "delivery.json", {"status": "prepared", "count": 21, "parts": parts,
               "sender": "sender@example.com", "snapshot_run_id": snapshot["run_id"], "as_of": snapshot["as_of"]})
    attempted = []

    def send(msg, cfg):
        attempted.append(str(msg["Message-ID"]))
        if len(attempted) == 2:
            raise smtplib.SMTPConnectError(421, b"temporary")

    monkeypatch.setattr(job, "send", send)
    monkeypatch.setattr(job, "load_dashboard", lambda *a: pytest.fail("prepared mail must resume without database access"))
    assert job.deliver_once(datetime(2026, 11, 16, 18))["status"] == "retry"
    assert job.deliver_once(datetime(2026, 11, 16, 18, 10))["status"] == "sent"
    assert len(attempted) == 3 and attempted[0] != attempted[1] == attempted[2]
    for file in root.rglob("*.eml"):
        msg = BytesParser(policy=policy.default).parsebytes(file.read_bytes())
        assert len(file.read_bytes()) <= job.MAX_MESSAGE_BYTES
        assert msg["To"] == "reader@example.com"


def test_47_candidates_fit_in_one_daily_email(delivery, monkeypatch):
    snapshot, _ = delivery
    snapshot["data"] = [row(f"{n:06d}.SH") for n in range(47)]
    sent = []
    monkeypatch.setattr(job, "send", lambda msg, cfg: sent.append(msg))
    assert job.deliver_once(datetime(2026, 11, 16, 18))["count"] == 47
    assert len(sent) == 1
    assert "候选 47只" in str(sent[0]["Subject"])
    assert "(1/" not in str(sent[0]["Subject"])
    assert "第1/1部分" not in sent[0].get_body(preferencelist=("html",)).get_content()
    images = [p for p in sent[0].walk() if p.get_content_type() == "image/png"]
    assert len(images) == len({str(p["Content-ID"]) for p in images}) == 47


def test_oversized_digest_never_splits_or_sends_partial_candidates(delivery, monkeypatch):
    snapshot, root = delivery
    snapshot["data"] = [row(f"{n:06d}.SH") for n in range(47)]
    monkeypatch.setattr(job, "MAX_MESSAGE_BYTES", 1000)
    monkeypatch.setattr(job, "send", lambda *a: pytest.fail("oversized mail must not send"))
    result = job.deliver_once(datetime(2026, 11, 16, 18))
    assert result["status"] == "blocked" and result["count"] == 47
    assert "未拆分、未发送" in result["reason"]
    assert not list(root.rglob("*.eml"))


def test_previously_sent_three_parts_are_not_rebuilt_or_resent(delivery, monkeypatch):
    _, root = delivery
    profile = job._profile("reader@example.com", 60., True)
    write_json(root / "2026-11-16" / profile / "delivery.json", {
        "status": "sent", "count": 47,
        "parts": [{"status": "sent", "file": f"part-{n:02d}.eml"} for n in range(1, 4)]})
    monkeypatch.setattr(job, "load_dashboard", lambda *a, **kw: pytest.fail("sent batch must not rebuild"))
    monkeypatch.setattr(job, "send", lambda *a: pytest.fail("sent batch must not resend"))
    assert job.deliver_once(datetime(2026, 11, 16, 18, 30)) == {
        "status": "sent", "already_processed": True, "count": 47}


def test_interrupted_sending_record_requires_review(delivery, monkeypatch):
    _, root = delivery
    profile = job._profile("reader@example.com", 60., True)
    write_json(root / "2026-11-16" / profile / "delivery.json", {
        "status": "prepared", "parts": [{"status": "sending"}], "count": 1})
    monkeypatch.setattr(job, "send", lambda *a: pytest.fail("unknown receipt"))
    assert job.deliver_once(datetime(2026, 11, 16, 18))["status"] == "uncertain"


def test_deadline_notice_is_not_a_stale_recommendation(delivery, monkeypatch):
    snapshot, _ = delivery
    snapshot["as_of"] = "2026-11-13 15:00:00"
    calls = []
    monkeypatch.setattr(job, "send", lambda msg, cfg: calls.append(msg))
    result = job.deliver_once(datetime(2026, 11, 16, 20), delay_notice=True)
    assert result["status"] == "sent" and result["delay_notice"] and result["count"] == 0
    assert not any(p.get_content_type() == "image/png" for p in calls[0].walk())
    assert "行情更新延迟" in str(calls[0]["Subject"])


def test_preview_never_calls_smtp(delivery, monkeypatch):
    monkeypatch.setattr(job, "send", lambda *a: pytest.fail("preview never sends"))
    result = job.preview()
    assert result["status"] == "preview" and result["sent"] is False
    assert result["count"] == 1


def test_permanent_server_rejection_stops_the_wait_cycle(delivery, monkeypatch):
    def reject(*args):
        raise smtplib.SMTPDataError(550, b"permanent refusal")

    monkeypatch.setattr(job, "send", reject)
    assert job.deliver_once(datetime(2026, 11, 16, 18))["status"] == "blocked"


def test_corrupt_delivery_ledger_does_not_become_an_unsent_day(delivery, monkeypatch):
    _, root = delivery
    profile = job._profile("reader@example.com", 60., True)
    path = root / "2026-11-16" / profile / "delivery.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(job, "send", lambda *a: pytest.fail("corrupt receipt must not resend"))
    with pytest.raises(ValueError):
        job.deliver_once(datetime(2026, 11, 16, 18))


def test_cached_mail_cannot_change_the_authorized_recipient(delivery, monkeypatch):
    _, root = delivery

    def disconnected(*args):
        raise smtplib.SMTPConnectError(421, b"retry")

    monkeypatch.setattr(job, "send", disconnected)
    assert job.deliver_once(datetime(2026, 11, 16, 18))["status"] == "retry"
    path = next(root.rglob("*.eml"))
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    msg.replace_header("To", "other@example.com")
    path.write_bytes(msg.as_bytes(policy=policy.SMTP))
    monkeypatch.setattr(job, "send", lambda *a: pytest.fail("recipient changed"))
    assert job.deliver_once(datetime(2026, 11, 16, 18, 10))["status"] == "blocked"
