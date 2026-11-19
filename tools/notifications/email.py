"""TLS SMTP transport and CID images, independent of the market domain."""
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from hashlib import sha256
import smtplib
import ssl
import time

from core.config import get_config


class MailNotConfigured(ValueError):
    pass


class DeliveryUncertain(RuntimeError):
    """The server may have accepted DATA; automatic resending risks duplicates."""


def address(value):
    if not value or any(c in value for c in "\r\n,;"):
        raise ValueError("邮箱地址无效")
    name, mailbox = parseaddr(value)
    if name or mailbox != value or mailbox.count("@") != 1 or not all(mailbox.split("@")):
        raise ValueError("请填写单个完整邮箱地址")
    return mailbox


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    sender: str
    security: str = "ssl"
    data_timeout: float = 120

    @classmethod
    def load(cls):
        cfg = get_config()
        values = {key: cfg.env(f"SMTP_{key.upper()}", "").strip()
                  for key in ("host", "user", "password", "from")}
        missing = [f"SMTP_{key.upper()}" for key in ("host", "user", "password") if not values[key]]
        if missing:
            raise MailNotConfigured("缺少发件配置: " + ", ".join(missing))
        security = cfg.env("SMTP_SECURITY", "ssl").lower()
        if security not in ("ssl", "starttls"):
            raise MailNotConfigured("SMTP_SECURITY仅支持ssl或starttls")
        port = int(cfg.env("SMTP_PORT", "465" if security == "ssl" else "587"))
        if not 1 <= port <= 65535:
            raise MailNotConfigured("SMTP_PORT无效")
        data_timeout = float(cfg.env("SMTP_DATA_TIMEOUT", "120"))
        if not 30 <= data_timeout <= 300:
            raise MailNotConfigured("SMTP_DATA_TIMEOUT必须为30~300秒")
        return cls(values["host"], port, values["user"], values["password"],
                   address(values["from"] or values["user"]), security, data_timeout)


def message(sender, recipient, subject, text, html, images, message_id, csv=None):
    msg = EmailMessage()
    msg["From"] = f"Alpha Finance Radar <{address(sender)}>"
    msg["To"] = address(recipient)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    if images:
        related = msg.get_payload()[-1]
        for cid, png in images.items():
            related.add_related(png, maintype="image", subtype="png", cid=f"<{cid}>", disposition="inline")
        body_hash = sha256(message_id.encode()).hexdigest()[:24]
        body_id = f"<body.{body_hash}@afr.invalid>"
        related.get_payload(0)["Content-ID"] = body_id
        related.set_param("type", "text/html")
        related.set_param("start", body_id)
    if csv is not None:
        msg.add_attachment(csv.encode("utf-8-sig"), maintype="text", subtype="csv", filename="stocks.csv")
    return msg


def _monitor_data(client, timeout):
    """Observe protocol stages without logging mail content or AUTH commands."""
    state = {"phase": "envelope", "uploaded_bytes": 0, "payload_bytes": 0, "final_chunk_started": False}
    original_data, original_send = getattr(client, "data", None), getattr(client, "send", None)
    if original_data is None or original_send is None:
        return {"phase": "data_reply"}  # Conservative for custom transports.

    def data(payload):
        state["phase"] = "data_prompt"
        result = original_data(payload)
        state["phase"] = "accepted" if result[0] == 250 else "rejected"
        return result

    def write(payload):
        if state["phase"] != "data_prompt" or not isinstance(payload, bytes):
            return original_send(payload)
        state["phase"] = "data_upload"
        state["payload_bytes"] = len(payload)
        deadline = time.monotonic() + timeout
        for offset in range(0, len(payload), 16384):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("SMTP正文上传超时")
            if getattr(client, "sock", None) is not None:
                client.sock.settimeout(remaining)
            state["final_chunk_started"] = offset+16384 >= len(payload)
            original_send(payload[offset:offset+16384])
            state["uploaded_bytes"] = min(offset+16384, len(payload))
        if getattr(client, "sock", None) is not None:
            client.sock.settimeout(timeout)
        state["phase"] = "data_reply"

    client.data, client.send = data, write
    return state


def send(msg, settings):
    """Return only after SMTP acceptance. Never log credentials or server text."""
    context = ssl.create_default_context()
    if settings.security == "ssl":
        client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=30, context=context)
    else:
        client = smtplib.SMTP(settings.host, settings.port, timeout=30)
    try:
        if settings.security == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(settings.username, settings.password)
        if getattr(client, "sock", None) is not None:
            # Large CID image messages and server-side scanning need a longer
            # DATA/ack deadline; connection and login retain their 30s timeout.
            client.sock.settimeout(settings.data_timeout)
        stage = _monitor_data(client, settings.data_timeout)
        try:
            refused = client.send_message(msg, from_addr=settings.sender, to_addrs=[str(msg["To"])])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        except smtplib.SMTPResponseException:
            raise  # An explicit rejection is safe to retry according to job policy.
        except smtplib.SMTPRecipientsRefused:
            raise
        except Exception as error:
            error.smtp_phase = stage["phase"]
            error.smtp_transfer = {key: stage.get(key) for key in ("uploaded_bytes", "payload_bytes", "final_chunk_started")}
            chain, current = [], error
            while current is not None and len(chain) < 5:
                chain.append(type(current).__name__)
                current = current.__cause__ or current.__context__
            error.smtp_error_chain = chain
            if stage["phase"] in ("envelope", "data_prompt") or (stage["phase"] == "data_upload" and not stage.get("final_chunk_started", True)):
                raise  # No body could have been accepted; retry is safe.
            uncertain = DeliveryUncertain("发送确认中断，请人工核对收件箱后处理，未自动重发")
            uncertain.phase = stage["phase"]
            uncertain.transfer = {key: stage.get(key) for key in ("uploaded_bytes", "payload_bytes", "final_chunk_started")}
            raise uncertain from error
    finally:
        # A disconnect during QUIT must not turn a successful DATA acceptance into
        # an apparent failure and a duplicate on the next scheduled attempt.
        try:
            client.close()
        except OSError:
            pass
