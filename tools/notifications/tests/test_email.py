import smtplib

import pytest

from tools.notifications import email


def build():
    return email.message("sender@example.com", "reader@example.com", "日报", "纯文本", '<img src="cid:stock-a">',
                         {"stock-a": b"png"}, "<digest@example.com>", csv="代码\n600000.SH")


def test_inline_images_are_related_to_html_and_csv_has_utf8_bom():
    msg = build()
    assert msg.get_body(preferencelist=("html",)).get_content().find("cid:stock-a") >= 0
    images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(images) == 1 and images[0]["Content-ID"] == "<stock-a>"
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
