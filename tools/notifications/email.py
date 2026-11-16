"""TLS SMTP transport and CID images, independent of the market domain."""
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
import smtplib
import ssl

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
        return cls(values["host"], port, values["user"], values["password"],
                   address(values["from"] or values["user"]), security)


def message(sender, recipient, subject, text, html, images, message_id, csv=None):
    msg = EmailMessage()
    msg["From"] = f"Alpha Finance Radar <{address(sender)}>"
    msg["To"] = address(recipient)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for cid, png in images.items():
        msg.get_payload()[-1].add_related(png, maintype="image", subtype="png", cid=f"<{cid}>",
                                        filename=f"{cid}.png", disposition="inline")
    if csv is not None:
        msg.add_attachment(csv.encode("utf-8-sig"), maintype="text", subtype="csv", filename="stocks.csv")
    return msg


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
        try:
            refused = client.send_message(msg, from_addr=settings.sender, to_addrs=[str(msg["To"])])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        except smtplib.SMTPResponseException:
            raise  # An explicit rejection is safe to retry according to job policy.
        except smtplib.SMTPRecipientsRefused:
            raise
        except Exception as error:
            raise DeliveryUncertain("发送确认中断，请人工核对收件箱后处理，未自动重发") from error
    finally:
        # A disconnect during QUIT must not turn a successful DATA acceptance into
        # an apparent failure and a duplicate on the next scheduled attempt.
        try:
            client.close()
        except OSError:
            pass
