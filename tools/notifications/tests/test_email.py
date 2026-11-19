import smtplib
from email import policy
from email.parser import BytesParser

import pytest

from tools.notifications import email


def build():
    return email.message("sender@example.com", "reader@example.com", "日报", "纯文本", '<img src="cid:stock-a">',
                         {"stock-a": b"png"}, "<digest@example.com>", csv="代码\n600000.SH")


def test_inline_images_are_related_to_html_and_csv_has_utf8_bom():
    msg = BytesParser(policy=policy.default).parsebytes(build().as_bytes(policy=policy.SMTP))
    body = msg.get_body(preferencelist=("html",))
    related = next(part for part in msg.walk() if part.get_content_type() == "multipart/related")
    assert related.get_param("type") == "text/html"
    assert related.get_param("start") == body["Content-ID"]
    assert "cid:stock-a" in body.get_content()
    images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(images) == 1 and images[0]["Content-ID"] == "<stock-a>"
    assert images[0].get_content_disposition() == "inline" and images[0].get_filename() is None
    csv = next(p for p in msg.walk() if p.get_filename() == "stocks.csv")
    assert csv.get_payload(decode=True).startswith(b"\xef\xbb\xbf")
    with pytest.raises(ValueError):
        email.address("a@example.com\r\nBcc: attacker@example.com")


@pytest.mark.parametrize("failure", [TimeoutError(), smtplib.SMTPServerDisconnected(), RuntimeError()])
def test_lost_data_acknowledgement_never_becomes_automatic_retry(monkeypatch, failure):
    class Client:
        def login(self, *args):
            pass

        def send_message(self, *args, **kwargs):
            raise failure

        def close(self):
            pass

    monkeypatch.setattr(email.smtplib, "SMTP_SSL", lambda *a, **kw: Client())
    with pytest.raises(email.DeliveryUncertain):
        email.send(build(), email.SmtpSettings("smtp.example.com", 465, "sender@example.com", "secret", "sender@example.com"))


def test_explicit_rejection_is_not_uncertain_and_close_failure_does_not_mask_acceptance(monkeypatch):
    class Client:
        reject = True

        def login(self, *args):
            pass

        def send_message(self, *args, **kwargs):
            if self.reject:
                raise smtplib.SMTPDataError(450, b"temporary rejection")
            return {}

        def close(self):
            raise OSError("socket already closed")

    client = Client()
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", lambda *a, **kw: client)
    settings = email.SmtpSettings("smtp.example.com", 465, "sender@example.com", "secret", "sender@example.com")
    with pytest.raises(smtplib.SMTPDataError):
        email.send(build(), settings)
    client.reject = False
    email.send(build(), settings)
    assert "secret" not in repr(settings)


def test_image_data_and_ack_timeout_is_separate_from_connection_timeout(monkeypatch):
    seen = []

    class Client:
        sock = None

        def login(self, *args):
            from types import SimpleNamespace
            self.sock = SimpleNamespace(settimeout=lambda value: seen.append(("data", value)))

        def send_message(self, *args, **kwargs):
            return {}

        def close(self):
            pass

    def connect(*args, **kwargs):
        seen.append(("connect", kwargs["timeout"]))
        return Client()

    monkeypatch.setattr(email.smtplib, "SMTP_SSL", connect)
    email.send(build(), email.SmtpSettings("smtp.example.com", 465, "sender@example.com", "secret", "sender@example.com"))
    assert seen == [("connect", 30), ("data", 120)]


def test_body_is_chunked_without_changing_bytes(monkeypatch):
    payload = b"x"*70000+b"\r\n.\r\n"
    chunks = []

    class Client:
        sock = None

        def login(self, *args):
            pass

        def send(self, value):
            chunks.append(value)

        def data(self, value):
            self.send(value)
            return 250, b"queued"

        def send_message(self, *args, **kwargs):
            self.data(payload)
            return {}

        def close(self):
            pass

    monkeypatch.setattr(email.smtplib, "SMTP_SSL", lambda *a, **kw: Client())
    email.send(build(), email.SmtpSettings("smtp.example.com", 465, "sender@example.com", "secret", "sender@example.com"))
    assert b"".join(chunks) == payload
    assert max(map(len, chunks)) <= 16384


def test_disconnect_before_data_is_retryable_not_uncertain(monkeypatch):
    class Client:
        def login(self, *args):
            pass

        def data(self, value):
            raise AssertionError("DATA never started")

        def send(self, value):
            pass

        def send_message(self, *args, **kwargs):
            raise smtplib.SMTPServerDisconnected("envelope failure")

        def close(self):
            pass

    monkeypatch.setattr(email.smtplib, "SMTP_SSL", lambda *a, **kw: Client())
    with pytest.raises(smtplib.SMTPServerDisconnected):
        email.send(build(), email.SmtpSettings("smtp.example.com", 465, "sender@example.com", "secret", "sender@example.com"))


@pytest.mark.parametrize("failed_chunk,uncertain", [(2, False), (5, True)])
def test_upload_failure_is_only_uncertain_when_terminator_could_have_been_sent(monkeypatch, failed_chunk, uncertain):
    class Client:
        sock = None
        count = 0

        def login(self, *args):
            pass

        def send(self, value):
            self.count += 1
            if self.count == failed_chunk:
                raise smtplib.SMTPServerDisconnected("upload interrupted")

        def data(self, value):
            self.send(value)
            return 250, b"queued"

        def send_message(self, *args, **kwargs):
            self.data(b"x"*70000+b"\r\n.\r\n")
            return {}

        def close(self):
            pass

    monkeypatch.setattr(email.smtplib, "SMTP_SSL", lambda *a, **kw: Client())
    with pytest.raises(email.DeliveryUncertain if uncertain else smtplib.SMTPServerDisconnected):
        email.send(build(), email.SmtpSettings("smtp.example.com", 465, "sender@example.com", "secret", "sender@example.com"))
