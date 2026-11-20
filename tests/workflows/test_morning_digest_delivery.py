import asyncio
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import threading
from types import SimpleNamespace

import pytest

from core.local_state import file_lock, read_json
from skills.report import morning_brief
from tools.market.refresh_clock import SHANGHAI
from tools.notifications.email import DeliveryUncertain
from workflows.market import send_close_digest as delivery
from workflows.reports import morning_scheduler as scheduler
from workflows.reports import send_morning_digest as morning


def at(day=14, hour=7, minute=30):
    return datetime(2026, 11, day, hour, minute, tzinfo=SHANGHAI)


@pytest.fixture
def edition(tmp_path, monkeypatch):
    cfg = SimpleNamespace(env=lambda k, default: "reader@example.test" if k == "MARKET_DIGEST_TO" else default,
                          get=lambda k, default: default)
    monkeypatch.setattr(morning, "get_config", lambda: cfg)
    monkeypatch.setattr(morning, "output_root", lambda: tmp_path)
    monkeypatch.setattr(morning.SmtpSettings, "load", lambda: SimpleNamespace(sender="sender@example.test"))
    monkeypatch.setattr(morning, "render_morning_mail", lambda b, **kwargs: ("周六研报", "报告及原文", "<p>研报原文</p>"))
    monkeypatch.setattr(morning, "needs_attachment", lambda b: True)
    calls, sent = [], []

    def load(day):
        calls.append(day)
        end = datetime.combine(day, morning.SEND_TIME, SHANGHAI)
        return {"window_end": end, "window_start": end - timedelta(days=1), "total_count": 3}

    monkeypatch.setattr(morning, "load_morning_brief", load)
    monkeypatch.setattr(delivery, "send", lambda msg, settings: sent.append(msg))
    return calls, sent


def test_local_preview_has_readable_mime_and_never_loads_smtp(edition, tmp_path, monkeypatch):
    monkeypatch.setattr(morning, "market_time", lambda: at(hour=6))
    monkeypatch.setattr(morning.SmtpSettings, "load", lambda: pytest.fail("preview used SMTP settings"))
    result = morning.preview()
    assert result["sent"] is False and edition == ([date(2026, 11, 13)], [])
    msg = BytesParser(policy=policy.default).parsebytes((tmp_path / "preview/2026-11-13/part-01.eml").read_bytes())
    assert "研报原文" in msg.get_body(("html",)).get_content()
    assert msg.get_body(("plain",)).get_content().strip() == "报告及原文"
    attachment, = list(msg.iter_attachments())
    assert attachment.get_payload(decode=True).decode("utf-8-sig") == "报告及原文"
    assert not list(tmp_path.rglob("delivery.json"))


def test_saturday_shanghai_slot_disabled_and_stale_date_guard(edition, monkeypatch):
    before = datetime(2026, 11, 13, 23, 29, tzinfo=timezone.utc)
    assert morning.deliver_once(before)["status"] == "not_due"
    assert morning.deliver_once(at(), issue_date=date(2026, 11, 13))["status"] == "not_due"
    assert morning.deliver_once(before + timedelta(minutes=1))["status"] == "sent"
    monkeypatch.setattr(morning, "enabled", lambda: False)
    assert morning.deliver_once(at(day=15))["status"] == "disabled"
    assert len(edition[0]) == len(edition[1]) == 1


def test_manual_sample_uses_rolling_window_and_keeps_official_editions_independent(edition, tmp_path, monkeypatch):
    cutoff = at(hour=6, minute=12)
    windows = []

    def empty_window(start, end):
        windows.append((start, end))
        return []

    monkeypatch.setattr(morning_brief, "reports_ingested_between", empty_window)
    calls, messages = edition
    assert morning.deliver_once(cutoff, sample=True)["status"] == "sent"
    assert morning.deliver_once(at(hour=18), sample=True)["already_processed"]
    assert windows == [(cutoff - timedelta(days=1), cutoff)]
    receipt, = (tmp_path / "sample" / cutoff.date().isoformat()).glob("*/delivery.json")
    assert read_json(receipt)["as_of"] == cutoff.isoformat()

    assert morning.deliver_once(at())["status"] == "sent"
    assert morning.deliver_once(at(day=15))["status"] == "sent"
    assert calls == [at().date(), at(day=15).date()]
    assert len({str(msg["Message-ID"]) for msg in messages}) == len(messages) == 3


def test_retry_reuses_frozen_email_and_duplicate_run_reads_receipt(edition, tmp_path, monkeypatch):
    calls, sent = edition
    ids = []

    def send(msg, settings):
        ids.append(str(msg["Message-ID"]))
        if len(ids) == 1:
            raise TimeoutError("failure before SMTP DATA")
        sent.append(msg)

    monkeypatch.setattr(delivery, "send", send)
    assert morning.deliver_once(at())["status"] == "retry"
    assert morning.deliver_once(at(hour=9))["status"] == "sent"
    assert morning.deliver_once(at(hour=19))["already_processed"]
    assert len(calls) == len(sent) == 1 and ids[0] == ids[1]
    receipt = read_json(next(tmp_path.rglob("delivery.json")))
    assert receipt["as_of"] == at().isoformat() and receipt["status"] == "sent"


def test_ambiguous_acknowledgement_and_cross_process_lock(edition, tmp_path, monkeypatch):
    def send(msg, settings):
        edition[1].append(msg)
        raise DeliveryUncertain()

    monkeypatch.setattr(delivery, "send", send)
    assert morning.deliver_once(at())["status"] == "uncertain"
    assert morning.deliver_once(at(hour=12))["status"] == "uncertain"
    with file_lock(next(tmp_path.rglob("delivery.lock"))) as acquired:
        assert acquired and morning.deliver_once(at())["status"] == "busy"
    assert len(edition[1]) == 1


def test_cli_wait_does_not_cross_midnight(monkeypatch):
    now = at(hour=23, minute=59)
    calls, sleeps = [], []
    monkeypatch.setattr(morning, "market_time", lambda: now)
    monkeypatch.setattr(morning, "attempt", lambda day: calls.append(day) or {"status": "retry"})

    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += timedelta(seconds=seconds)

    monkeypatch.setattr(morning, "sleep", sleep)
    assert morning.deliver_today(wait=True) == {"status": "retry"}
    assert sleeps == [60] and calls == [at().date()]


def test_suspended_edition_preparation_cannot_send_yesterdays_mail(edition, monkeypatch):
    now = at(hour=23, minute=59)
    monkeypatch.setattr(morning, "market_time", lambda value=None: now)
    prepare = morning._prepare_edition

    def slow(*args, **kwargs):
        nonlocal now
        result = prepare(*args, **kwargs)
        now += timedelta(minutes=5)
        return result

    monkeypatch.setattr(morning, "_prepare_edition", slow)
    assert morning.deliver_once()["status"] == "not_due" and not edition[1]


def test_new_installation_after_slot_starts_tomorrow_then_recovers_on_restart(edition):
    first = scheduler._first_issue(at(hour=18))
    assert first == at(day=15).date()
    assert scheduler._first_issue(at(day=15, hour=9)) == first


def test_scheduler_uses_worker_thread_and_retries_same_sunday_edition(edition, monkeypatch):
    scheduler._first_issue(at(hour=18))
    now = at(day=15, hour=9)
    calls, waits = [], []
    main_thread = threading.get_ident()
    monkeypatch.setattr(scheduler, "market_time", lambda: now)
    job = scheduler.ReportDigestScheduler()

    def attempt(day):
        assert day == now.date() and threading.get_ident() != main_thread
        calls.append(day)
        return {"status": "retry" if len(calls) == 1 else "sent"}

    async def wait(seconds):
        nonlocal now
        waits.append(job.next_run)
        if len(calls) == 1:
            now += timedelta(seconds=seconds)
        else:
            job._stop.set()
        return job._stop.is_set()

    monkeypatch.setattr(morning, "attempt", attempt)
    job._wait = wait
    asyncio.run(job._loop())
    assert len(calls) == 2 and waits == [at(day=15, hour=9, minute=40), at(day=16)]


def test_scheduler_first_start_waits_until_next_shanghai_slot(edition, monkeypatch):
    now = at(hour=17)
    monkeypatch.setattr(scheduler, "market_time", lambda: now)
    monkeypatch.setattr(morning, "attempt", lambda day: pytest.fail("first activation sent a past issue"))
    job = scheduler.ReportDigestScheduler()

    async def wait(seconds):
        assert job.next_run == at(day=15)
        job._stop.set()
        return True

    job._wait = wait
    asyncio.run(job._loop())
