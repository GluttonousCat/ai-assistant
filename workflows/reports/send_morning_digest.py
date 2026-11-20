"""Preview a Shanghai 07:30 research edition, or deliver today's edition once."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from email import policy
from hashlib import sha256
import json
from pathlib import Path
from time import sleep

from core.config import get_config
from core.local_state import write_json
from core.logger import get_logger
from skills.report.morning_brief import MORNING_CUTOFF as SEND_TIME, load_brief_ending_at, load_morning_brief
from skills.report.morning_mail import needs_attachment, render_morning_mail
from tools.market.refresh_clock import SHANGHAI, market_time
from tools.notifications.email import MailNotConfigured, SmtpSettings, address, message
from workflows.market.send_close_digest import MAX_MESSAGE_BYTES, deliver_prepared, write_prepared

logger = get_logger(__name__)
RETRY_SECONDS = 600


def enabled():
    cfg = get_config()
    value = cfg.env("REPORT_DIGEST_ENABLED", cfg.get("report_digest.enabled", True))
    return str(value).strip().lower() in ("true", "1", "yes")


def output_root():
    return Path(get_config().root_dir) / "output" / "reports" / "morning_email"


def _prepare_edition(day, folder, sender, recipient, *, preview_at=None, sample=False):
    brief = load_brief_ending_at(preview_at) if sample else load_morning_brief(day)
    if preview_at is not None:
        brief = {**brief, "preview_at": preview_at, "sample": sample}
    subject, text, html = render_morning_mail(brief)
    fingerprint = sha256(recipient.encode()).hexdigest()[:16]
    namespace = "afr.research.sample" if sample else "afr.research"
    message_id = f"<{namespace}.{day}.{fingerprint}@{sender.split('@')[1]}>"
    msg = message(sender, recipient, subject, text, html, {}, message_id)
    if needs_attachment(brief):
        msg.add_attachment(text.encode("utf-8-sig"), maintype="text", subtype="plain",
                           filename=f"研报观点与原文-{day}.txt")
    payload = msg.as_bytes(policy=policy.SMTP)
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("研报早报超过单封邮件大小上限")
    parts = write_prepared(folder, html, payload, message_id, {})
    return {"status": "prepared", "prepared_at": market_time().isoformat(), "as_of": brief["window_end"].isoformat(),
            "window_start": brief["window_start"].isoformat(), "count": brief["total_count"], "parts": parts}


def preview(day=None):
    now = market_time()
    # The most recent completed 24-hour window makes a meaningful preview before 07:30.
    day = day or (now.date() if now.time() >= SEND_TIME else now.date() - timedelta(days=1))
    folder = output_root() / "preview" / day.isoformat()
    state = _prepare_edition(day, folder, "radar@example.invalid", "preview@example.invalid", preview_at=now)
    result = {"status": "preview", "sent": False, "count": state["count"],
              "as_of": state["as_of"], "window_start": state["window_start"],
              "html": str(folder / "part-01.html"), "eml": str(folder / "part-01.eml")}
    write_json(folder / "manifest.json", result)
    return result


def deliver_once(now=None, *, issue_date=None, sample=False):
    live_clock = now is None
    now = market_time(now)
    day = now.date()
    if not enabled():
        return {"status": "disabled"}
    if (not sample and now.time() < SEND_TIME) or (issue_date is not None and issue_date != day):
        return {"status": "not_due"}
    cfg = get_config()
    try:
        recipient = address(cfg.env("REPORT_DIGEST_TO", "").strip() or cfg.env("MARKET_DIGEST_TO", "").strip())
        settings = SmtpSettings.load()
    except MailNotConfigured as exc:
        return {"status": "blocked", "reason": str(exc)}
    except ValueError:
        return {"status": "blocked", "reason": "研报收件人或发件配置无效"}

    profile = sha256(recipient.encode()).hexdigest()[:16]
    root = output_root() / "sample" if sample else output_root()
    folder = root / day.isoformat() / profile

    def prepare(_state, destination):
        edition = _prepare_edition(day, destination, settings.sender, recipient,
                                   preview_at=now if sample else None, sample=sample)
        if live_clock and market_time().date() != day:
            return {"status": "not_due"}
        return edition

    try:
        return deliver_prepared(folder, recipient, settings, prepare, now=now, test=sample)
    except (ValueError, KeyError, TypeError) as exc:
        return {"status": "blocked", "error_type": type(exc).__name__, "reason": "早报内容或本地发送记录无效"}


def attempt(day):
    try:
        return deliver_once(issue_date=day)
    except Exception as exc:
        logger.warning("研报早报准备失败: %s", type(exc).__name__)
        return {"status": "retry", "error_type": type(exc).__name__}


def deliver_today(*, wait=False):
    day = market_time().date()
    midnight = datetime.combine(day + timedelta(days=1), time.min, SHANGHAI)
    while True:
        result = attempt(day)
        remaining = (midnight - market_time()).total_seconds()
        if not wait or result["status"] not in ("retry", "busy") or remaining <= 0:
            return result
        sleep(min(RETRY_SECONDS, remaining))
        if market_time().date() != day:
            return result


def main():
    parser = argparse.ArgumentParser(description="生成研报观点早报的本地预览，或发送今天的正式早报")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--day", type=date.fromisoformat, help="预览指定日期的 07:30 截止期")
    group.add_argument("--send", action="store_true", help="北京时间 07:30 后实际发送当期早报")
    group.add_argument("--send-sample", action="store_true", help="立即发送截至当前的近24小时样刊，按天单独记回执")
    parser.add_argument("--wait", action="store_true", help="明确失败或发送锁忙时在当天每十分钟重试")
    args = parser.parse_args()
    if args.wait and not args.send:
        parser.error("--wait 需要与 --send 一起使用")
    try:
        if args.send_sample:
            result = deliver_once(sample=True)
        elif args.send:
            result = deliver_today(wait=args.wait)
        else:
            result = preview(args.day)
    except Exception as exc:
        logger.error("研报早报任务失败: %s", type(exc).__name__)
        result = {"status": "error", "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in ("sent", "preview", "not_due", "disabled") else 2


if __name__ == "__main__":
    raise SystemExit(main())
