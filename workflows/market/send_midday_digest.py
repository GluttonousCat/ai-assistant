"""Build or send the independent 11:55 backend email."""
from datetime import date, datetime, time
from email import policy
from hashlib import sha256
import json

from core.config import get_config
from core.local_state import read_json, write_json
from workflows.market.send_close_digest import MAX_MESSAGE_BYTES, _root, deliver_prepared, minimum_score, write_prepared
from skills.trend.intraday_digest import render
from tools.kline.calendar import is_trading_day
from tools.market.refresh_clock import SHANGHAI, market_time
from tools.market.trend_data import intraday_report
from tools.notifications.email import MailNotConfigured, SmtpSettings, address, message

SEND_TIME = time(11, 55)
RETRY_UNTIL = time(13, 30)


def _morning_report(day):
    run_id = f"{day}_1130"
    report = intraday_report(run_id)
    if not report or report.get("run_id") != run_id or report.get("as_of") != f"{day} 11:30:00":
        return None
    opening = intraday_report(f"{day}_1030")
    if (opening and opening.get("as_of") == f"{day} 10:30:00"
            and all(opening.get(k) == report.get(k) for k in ("source_snapshot_run_id", "strategy_version", "config"))):
        return {**report, "opening_follow_up": opening.get("follow_up", [])}
    return report


def _serialize(report, recipient, sender, *, preview=False):
    html, text, csv, counts = render(report, minimum=minimum_score())
    subject = f"{'[预览] ' if preview else ''}Alpha Finance Radar | {report['as_of'][:10]} | 午间60F简报"
    key = sha256(f"{recipient}|{report['run_id']}".encode()).hexdigest()[:24]
    message_id = f"<afr.midday.{key}@{sender.split('@')[1]}>"
    msg = message(sender, recipient, subject, text, html, {}, message_id, csv=csv)
    return html, msg.as_bytes(policy=policy.SMTP), message_id, counts


def deliver_midday_once(now=None, *, deadline_notice=False):
    now = market_time(now)
    day, clock = now.date(), now.time()
    if not is_trading_day(day) or clock < SEND_TIME or (clock > RETRY_UNTIL and not deadline_notice):
        return {"status": "not_due"}
    recipient = address(get_config().env("MARKET_DIGEST_TO", ""))
    try:
        settings = SmtpSettings.load()
    except MailNotConfigured as error:
        return {"status": "blocked", "reason": str(error)}
    profile = sha256(recipient.encode()).hexdigest()[:16]
    folder = _root() / "midday" / day.isoformat() / profile

    def prepare(state, output):
        report = _morning_report(day)
        if report:
            html, payload, message_id, counts = _serialize(report, recipient, settings.sender)
        elif deadline_notice and clock >= RETRY_UNTIL:
            subject = f"Alpha Finance Radar | {day} | 午间行情更新延迟"
            text = f"{day} 11:30 的早盘扫描仍未完成，本期暂无可核验的跟踪变化和新观察。"
            html = f'<html lang="zh-CN"><body><h2>午间行情更新延迟</h2><p>{text}</p></body></html>'
            message_id = f"<afr.midday.delay.{day}.{profile}@{settings.sender.split('@')[1]}>"
            payload = message(settings.sender, recipient, subject, text, html, {}, message_id).as_bytes(policy=policy.SMTP)
            counts = {}
        else:
            return {"status": "waiting_data", "target": f"{day} 11:30"}
        if len(payload) > MAX_MESSAGE_BYTES:
            return {"status": "blocked", "reason": "午间邮件超过大小上限"}
        parts = write_prepared(output, html, payload, message_id, {})
        return {"status": "prepared", "snapshot_run_id": report["run_id"] if report else None,
                "as_of": report["as_of"] if report else None, "delay_notice": not report,
                "count": counts.get("new", 0), "parts": parts, "counts": counts}

    result = deliver_prepared(folder, recipient, settings, prepare)
    if result["status"] == "sent":
        receipt = read_json(folder / "delivery.json", strict=True)
        result.update(counts=receipt.get("counts", {}), delay_notice=receipt.get("delay_notice", False))
    return result


def preview(day=None):
    day = day or datetime.now(SHANGHAI).date()
    report = _morning_report(day)
    if not report:
        raise ValueError(f"{day} 11:30 尚无已发布扫描")
    folder = _root() / "midday" / "preview" / day.isoformat()
    html, payload, message_id, counts = _serialize(report, "preview@example.invalid", "radar@example.invalid", preview=True)
    write_prepared(folder, html, payload, message_id, {})
    result = {"status": "preview", "as_of": report["as_of"], **counts, "html": str(folder / "part-01.html")}
    write_json(folder / "manifest.json", result)
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成午间60F邮件本地预览")
    parser.add_argument("--day", type=date.fromisoformat)
    print(json.dumps(preview(parser.parse_args().day), ensure_ascii=False))
