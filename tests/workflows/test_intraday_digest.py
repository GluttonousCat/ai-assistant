from datetime import date, datetime, timezone
from types import SimpleNamespace

from jobs import market_digest_scheduler, send_intraday_digest as midday, send_market_digest as delivery
from tools.notifications.email import SmtpSettings


def test_midday_starts_1155_shanghai_and_skips_weekends():
    scheduler = market_digest_scheduler.MiddayDigestScheduler()
    before = datetime(2026, 11, 20, 3, 54, tzinfo=timezone.utc)
    after = datetime(2026, 11, 20, 3, 56, tzinfo=timezone.utc)
    assert scheduler.next_slot(before).isoformat() == "2026-11-20T11:55:00+08:00"
    assert scheduler.next_slot(after).isoformat() == "2026-11-23T11:55:00+08:00"
    assert not scheduler.initial_due(before.astimezone(midday.SHANGHAI))
    assert scheduler.initial_due(after.astimezone(midday.SHANGHAI))


def test_midday_waits_for_exact_morning_round_and_only_sends_once(tmp_path, monkeypatch):
    report = {
        "run_id": "2026-11-19_1130", "as_of": "2026-11-19 11:30:00", "daily_as_of": "2026-11-18 15:00:00",
        "follow_up": [{"ts_code": "600001.SH", "name": "甲", "state": "盘中观察", "last_seen": "2026-11-18",
                       "events": ["跌破历史参考位"], "change_pct": -4.2, "support": 8.0}],
        "data": [{"ts_code": "600002.SH", "name": "乙", "state": "盘中观察", "reason": "低位回升",
                  "score": 77, "new_today": True, "trend60": {"origin": "2026-11-19T10:30:00"}}],
    }
    lookup, sent = {}, []
    monkeypatch.setattr(midday, "minimum_score", lambda: 72)
    report["data"].extend([{**report["data"][0], "ts_code": "600003.SH", "name": "边界样本", "score": 72},
                           {**report["data"][0], "ts_code": "600004.SH", "name": "低分样本", "score": 71.9}])
    monkeypatch.setattr(midday, "get_config", lambda: SimpleNamespace(env=lambda *args: "reader@example.com"))
    monkeypatch.setattr(midday, "_root", lambda: tmp_path)
    monkeypatch.setattr(midday, "intraday_report", lookup.get)
    monkeypatch.setattr(midday.SmtpSettings, "load",
                        lambda: SmtpSettings("smtp.example.com", 465, "sender@example.com", "", "sender@example.com"))
    monkeypatch.setattr(delivery, "send", lambda msg, settings: sent.append(msg))
    assert midday.deliver_midday_once(datetime(2026, 11, 19, 11, 54))["status"] == "not_due"
    now = datetime(2026, 11, 19, 11, 55)
    lookup[report["run_id"]] = {**report, "as_of": "2026-11-19 10:30:00"}
    assert midday.deliver_midday_once(now)["status"] == "waiting_data" and not sent
    lookup["2026-11-19_1030"] = {**report, "run_id": "2026-11-19_1030", "as_of": "2026-11-19 10:30:00",
                                 "follow_up": [{**report["follow_up"][0], "events": ["结构起点变化"]}]}
    lookup[report["run_id"]] = report
    assert midday.deliver_midday_once(now)["status"] == "sent"
    lookup.clear()
    assert midday.deliver_midday_once(now)["already_processed"]
    assert midday.deliver_midday_once(datetime(2026, 11, 19, 14, 0))["status"] == "not_due"
    assert len(sent) == 1
    html = sent[0].get_body(preferencelist=("html",)).get_content()
    assert "历史标的跟踪" in html and "跌破历史参考位" in html and "盘中新观察" in html and "低位回升" in html
    assert "10:30 结构起点变化" in html and "11:30 跌破历史参考位" in html
    assert "边界样本" in html and "低分样本" not in html and "≥ 72" in html
    assert list(tmp_path.glob("midday/*/*/delivery.json"))
    assert midday._morning_report(date(2026, 11, 20)) is None


def test_missing_morning_snapshot_at_deadline_sends_one_status_email(tmp_path, monkeypatch):
    monkeypatch.setattr(midday, "get_config", lambda: SimpleNamespace(env=lambda *args: "reader@example.com"))
    monkeypatch.setattr(midday, "_root", lambda: tmp_path)
    monkeypatch.setattr(midday, "intraday_report", lambda run_id: None)
    monkeypatch.setattr(midday.SmtpSettings, "load",
                        lambda: SmtpSettings("smtp.example.com", 465, "sender@example.com", "", "sender@example.com"))
    sent = []
    monkeypatch.setattr(delivery, "send", lambda msg, settings: sent.append(msg))
    now = datetime(2026, 11, 19, 13, 30, 1)
    result = midday.deliver_midday_once(now, deadline_notice=True)
    assert result["status"] == "sent" and result["delay_notice"]
    assert midday.deliver_midday_once(now, deadline_notice=True)["already_processed"]
    assert len(sent) == 1 and "午间行情更新延迟" in str(sent[0]["Subject"])
