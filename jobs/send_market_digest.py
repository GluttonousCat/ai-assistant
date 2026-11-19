"""Preview or deliver the daily visual trend email from a pinned local snapshot.

Scheduled entry: python -m jobs.send_market_digest --send --wait
Delivery is never inferred from rendering a preview or from a successful login.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, time as day_time
from email import policy
from email.parser import BytesParser
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import smtplib
import time
from zoneinfo import ZoneInfo

from core.config import get_config
from core.local_state import file_lock, read_json, write_json
from core.logger import get_logger
from skills.trend.digest import DEFAULT_EMAIL_MIN_SCORE, export_csv, render_html, select_stocks, stock_cards
from tools.market.refresh_clock import expected_date
from tools.market.refresh_state import read_state as market_state
from tools.market.trend_data import load_dashboard, scan_cutoffs
from tools.notifications.email import SmtpSettings, MailNotConfigured, DeliveryUncertain, address, message, send

logger = get_logger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def options():
    cfg = get_config()
    minimum = float(cfg.env("MARKET_DIGEST_MIN_SCORE", cfg.get("email_digest.min_score", DEFAULT_EMAIL_MIN_SCORE)))
    if not 0 <= minimum <= 100:
        raise ValueError("邮件评分门槛必须为0~100")
    observations = str(cfg.env("MARKET_DIGEST_OBSERVATIONS", cfg.get("email_digest.observations", True))).lower() in ("true", "1", "yes")
    recipient = address(cfg.env("MARKET_DIGEST_TO", ""))
    return recipient, minimum, observations


def _root():
    root = Path(get_config().root_dir)
    plot_cache = root / "output" / "tmp" / "matplotlib"
    plot_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(plot_cache))
    return root / "output" / "market" / "email_digest"


def _profile(recipient, minimum, observations):
    return sha256(f"{recipient}|{minimum}|{observations}".encode()).hexdigest()[:16]


def _mail_file(folder, filename):
    if not isinstance(filename, str) or not re.fullmatch(r"part-\d{2,}\.eml", filename):
        raise ValueError("发送记录中的信件文件名无效")
    return folder / filename


def ready(snapshot, target, status, bounds):
    target = target.isoformat()
    return bool(snapshot and str(snapshot["as_of"])[:10] == target
                and all(str(snapshot["metadata"].get("timeframes", {}).get(t, {}).get("as_of", ""))[:10] == target
                        for t in ("1D", "60F"))
                and status.get("status") == "complete" and status.get("target") == target
                and status.get("snapshot", {}).get("run_id") == snapshot["run_id"]
                and all(bounds.get(key) is not None and str(bounds[key])[:10] >= target
                        for key in ("bar_end", "daily_end", "factor_date")))


class DigestTooLarge(ValueError):
    """Do not turn one daily digest into several messages or omit candidates."""


def _message(snapshot, cards, images, report_day, minimum, recipient, sender, *, preview=False, delayed=False, observations=True, test=False):
    html = render_html(snapshot, cards, report_day, minimum, preview=preview, delayed=delayed, test=test)
    profile = _profile(recipient, minimum, observations)
    kind = "test" if test else "delay" if delayed else "digest"
    run = snapshot["run_id"] if snapshot else "none"
    message_id = f"<afr.{kind}.{report_day}.{profile}.{run}.1@{sender.split('@')[1]}>"
    subject = ("[测试] " if test else "[预览] " if preview else "") + f"Alpha Finance Radar | {report_day} | "
    subject += "行情更新延迟" if delayed else f">{minimum:g}分候选 {len(cards)}只"
    text = f"{subject}\n数据截止: {snapshot['as_of'] if snapshot else '暂无'}（上海时间）\n"
    text += "入场候选和观察候选的文字解读在前，日 K 与 60F 附图在后。\n"
    for card in cards:
        row = card["row"]
        text += f"\n{row['ts_code']} {row['name']} {row['score']}分 · {row['state']}\n"
        text += "\n".join(f"{title}　{content}" for title, content in card["notes"].items()) + "\n"
    msg = message(sender, recipient, subject, text, html, images,
                  message_id, csv=export_csv([c["row"] for c in cards]) if not delayed else None)
    payload = msg.as_bytes(policy=policy.SMTP)
    if len(payload) > MAX_MESSAGE_BYTES:
        raise DigestTooLarge("整封日报超过8MiB上限，未拆分、未发送，请检查预览")
    return html, payload, message_id


def _prepare(folder, snapshot, cards, report_day, minimum, recipient, sender, preview=False, delayed=False, observations=True, test=False):
    images = {chart["cid"]: chart["png"] for card in cards for chart in card["charts"].values()}
    html, payload, message_id = _message(
        snapshot, cards, images, report_day, minimum, recipient, sender,
        preview=preview, delayed=delayed, observations=observations, test=test,
    )
    folder.mkdir(parents=True, exist_ok=True)
    browser_html = html
    for cid, png in images.items():
        inline_image = base64.b64encode(png).decode("ascii")
        browser_html = browser_html.replace(f'src="cid:{cid}"', f'src="data:image/png;base64,{inline_image}"')
        (folder / f"{cid.split('.', 1)[0]}.png").write_bytes(png)
    (folder / "part-01.html").write_text(browser_html, encoding="utf-8")
    filename = "part-01.eml"
    (folder / filename).write_bytes(payload)
    return [{"file": filename, "status": "prepared", "message_id": message_id}]


def preview():
    recipient, minimum, observations = options()
    snapshot = load_dashboard()
    if not snapshot:
        raise ValueError("尚无已发布快照")
    folder = _root() / "preview" / snapshot["run_id"]
    rows = select_stocks(snapshot, minimum, observations, exclusive=True)
    cards = stock_cards(snapshot, rows)
    files = _prepare(folder, snapshot, cards, datetime.now(SHANGHAI).date(), minimum, recipient,
                     "radar@example.invalid", preview=True, observations=observations)
    result = {"status": "preview", "sent": False, "count": len(rows), "as_of": snapshot["as_of"],
              "snapshot_run_id": snapshot["run_id"], "parts": len(files), "html": str(folder / "part-01.html")}
    write_json(folder / "manifest.json", result)
    return result


def deliver_once(now=None, *, delay_notice=False, test=False, retry_uncertain_test=False):
    if retry_uncertain_test and not test:
        raise ValueError("仅允许在用户核对未收到后显式重试测试邮件")
    now = now or datetime.now(SHANGHAI)
    now = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    if not test and now.time().replace(tzinfo=None) < day_time(18):
        return {"status": "not_due", "next": str(now.date())+" 18:00 Asia/Shanghai"}
    recipient, minimum, observations = options()
    try:
        settings = SmtpSettings.load()
    except MailNotConfigured as error:
        return {"status": "blocked", "reason": str(error)}
    report_day = now.date()
    target = expected_date(now)
    profile = _profile(recipient, minimum, observations)
    root = (_root() / "test" if test else _root()) / str(report_day) / profile
    with file_lock(root / "delivery.lock") as acquired:
        if not acquired:
            return {"status": "busy"}
        state_path = root / ("delay.json" if delay_notice else "delivery.json")
        state = read_json(state_path, strict=True)
        if state.get("status") == "sent":
            return {"status": state["status"], "already_processed": True, "count": state.get("count")}
        # A crash after SMTP acceptance may leave 'sending'; do not risk a duplicate.
        uncertain = state.get("status") == "uncertain" or any(
            p.get("status") in ("sending", "uncertain") for p in state.get("parts", []))
        if uncertain and not retry_uncertain_test:
            state["status"] = "uncertain"
            write_json(state_path, state)
            return {"status": "uncertain", "reason": "上次发送确认中断，请核对收件箱；未自动重发"}
        if uncertain:
            state.setdefault("uncertain_history", []).append({
                "acknowledged_not_received_at": now.isoformat(),
                "error_type": state.get("error_type"),
                "error_phase": state.get("error_phase"),
                "parts": [dict(part) for part in state["parts"]],
            })
            for part in state["parts"]:
                if part["status"] in ("sending", "uncertain"):
                    part["status"] = "retry"
            if not any(part["status"] == "sent" for part in state["parts"]):
                # The user confirmed no receipt. Keep original wire messages
                # before regenerating this test with corrected image encoding.
                archive = root / "attempt-history" / str(len(state["uncertain_history"]))
                archive.mkdir(parents=True, exist_ok=True)
                for part in state["parts"]:
                    source = _mail_file(root / "report", part["file"])
                    if source.exists():
                        (archive / source.name).write_bytes(source.read_bytes())
                state["parts"] = []
            state["status"] = "prepared"
            state.pop("error_type", None)
            state.pop("error_phase", None)
            write_json(state_path, state)
        if not state.get("parts"):
            snapshot = load_dashboard(state["snapshot_run_id"]) if state.get("snapshot_run_id") else load_dashboard()
            # Explicit tests use the latest published snapshot at any hour; its
            # actual cutoff is displayed. Per-stock chart validation still runs.
            complete = bool(snapshot) if test else target is not None and ready(snapshot, target, market_state(), scan_cutoffs(now.replace(tzinfo=None)))
            if not complete and not delay_notice:
                return {"status": "waiting_data", "target": str(target), "as_of": snapshot.get("as_of") if snapshot else None}
            if complete and delay_notice:
                return {"status": "retry", "reason": "数据刚完成，请发送正式日报"}
            rows = [] if delay_notice else select_stocks(snapshot, minimum, observations, exclusive=True)
            cards = [] if delay_notice else stock_cards(snapshot, rows)
            try:
                parts = _prepare(root / ("delay" if delay_notice else "report"), snapshot, cards, report_day,
                                 minimum, recipient, settings.sender, delayed=delay_notice, observations=observations, test=test)
            except DigestTooLarge:
                return {"status": "blocked", "reason": "整封日报超过8MiB上限，未拆分、未发送", "count": len(rows)}
            state = {**state, "status": "prepared", "snapshot_run_id": snapshot["run_id"] if snapshot else None,
                     "as_of": snapshot["as_of"] if snapshot else None, "count": len(rows),
                     "sender": settings.sender, "parts": parts}
            write_json(state_path, state)
        if state["sender"] != settings.sender:
            return {"status": "blocked", "reason": "发件人已变更，请先核对已有发送记录"}
        for part in state["parts"]:
            if part["status"] == "sent":
                continue
            part["status"] = "sending"
            write_json(state_path, state)
            try:
                payload = _mail_file(root / ("delay" if delay_notice else "report"), part["file"]).read_bytes()
                msg = BytesParser(policy=policy.default).parsebytes(payload)
                if address(str(msg["To"])) != recipient or str(msg["Message-ID"]) != part["message_id"]:
                    raise ValueError("缓存信件收件人或标识与发送记录不一致")
                send(msg, settings)
            except DeliveryUncertain as error:
                part["status"], state["status"] = "uncertain", "uncertain"
                state["error_type"] = type(error.__cause__).__name__ if error.__cause__ else type(error).__name__
                state["error_phase"] = getattr(error, "phase", "unknown")
                state["transfer"] = getattr(error, "transfer", {})
                write_json(state_path, state)
                return {"status": "uncertain", "error_type": state["error_type"],
                        "error_phase": state["error_phase"],
                        "reason": "SMTP回执中断，未自动重发，请核对收件箱"}
            except Exception as error:
                # Never copy server responses, credentials or deployment details
                # into mail status, logs, or automation replies.
                part["status"], state["status"] = "retry", "retry"
                state["error_type"] = type(error).__name__
                state["error_phase"] = getattr(error, "smtp_phase", "before_data")
                state["transfer"] = getattr(error, "smtp_transfer", {})
                state["error_chain"] = getattr(error, "smtp_error_chain", [type(error).__name__])
                write_json(state_path, state)
                logger.warning("趋势邮件发送失败: %s", type(error).__name__)
                blocked = (isinstance(error, (ValueError, smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError))
                           or isinstance(error, smtplib.SMTPResponseException) and error.smtp_code >= 500)
                return {"status": "blocked" if blocked else "retry", "error_type": type(error).__name__,
                        "error_phase": state["error_phase"], "transfer": state["transfer"], "error_chain": state["error_chain"]}
            part["status"], part["sent_at"] = "sent", datetime.now(SHANGHAI).isoformat()
            write_json(state_path, state)
        state["status"], state["sent_at"] = "sent", datetime.now(SHANGHAI).isoformat()
        write_json(state_path, state)
        return {"status": "sent", "count": state["count"], "parts": len(state["parts"]),
                "as_of": state["as_of"], "delay_notice": delay_notice, "test": test}


def _main():
    parser = argparse.ArgumentParser(description="生成或发送盘后趋势图文邮件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true", help="实际发送正式日报；18点前不发")
    mode.add_argument("--test-send", action="store_true", help="显式发送测试邮件，独立记录，不占正式日报名额")
    parser.add_argument("--retry-uncertain-test", action="store_true", help="用户确认未收到后，保留旧回执记录并重试测试邮件")
    parser.add_argument("--wait", action="store_true", help="数据或网络延迟时每10分钟重试，最多等至20:00")
    args = parser.parse_args()
    if args.retry_uncertain_test and not args.test_send:
        parser.error("--retry-uncertain-test必须与--test-send一起使用")
    get_config()
    if args.test_send:
        if args.wait:
            parser.error("测试邮件不自动等待重试")
        result = deliver_once(test=True, retry_uncertain_test=args.retry_uncertain_test)
    elif not args.send:
        result = preview()
    else:
        deadline = datetime.combine(datetime.now(SHANGHAI).date(), day_time(20), SHANGHAI)
        while True:
            result = deliver_once()
            if result["status"] not in ("waiting_data", "retry", "busy") or not args.wait:
                break
            if datetime.now(SHANGHAI) >= deadline:
                if result["status"] == "waiting_data":
                    result = deliver_once(delay_notice=True)
                break
            # Bounded sleeps keep shutdown responsive; no HTTP thread is held.
            remaining = min(600, (deadline-datetime.now(SHANGHAI)).total_seconds())
            while remaining > 0:
                step = min(30, remaining)
                time.sleep(step)
                remaining -= step
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] in ("sent", "preview", "not_due") else 2


def main():
    try:
        return _main()
    except Exception as error:
        logger.error("趋势邮件任务失败: %s", type(error).__name__)
        print(json.dumps({"status": "error", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
