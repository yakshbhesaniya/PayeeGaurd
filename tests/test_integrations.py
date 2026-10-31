"""Live integrations with mocked services: safety rails hold, demo mode is the default."""

import email.message

import pytest

from payeeguard import integrations

KEYS = ["IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_PORT",
        "SAFE_RECIPIENT", "EMAIL_TO_VENDOR", "STRIPE_SECRET_KEY", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET",
        "ALERT_WEBHOOK_URL", "JUSPAY_API_KEY", "JUSPAY_MERCHANT_ID", "JUSPAY_BASE_URL"]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in KEYS:
        monkeypatch.setenv(k, "")


def test_demo_mode_by_default():
    st = integrations.status()
    assert not any(st[k] for k in ("inbox", "email", "payments", "alerts"))
    assert integrations.create_payment("c1", 10, "USD", "V")["ok"] is False
    assert integrations.send_email("a@b.com", "s", "b")["ok"] is False
    assert integrations.alert("x") is None
    assert integrations.fetch_unseen() == []


def test_live_payment_keys_refused(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    r = integrations.create_payment("c1", 10, "USD", "V")
    assert not r["ok"] and "TEST" in r["message"]


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


def test_stripe_test_payment(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    seen = {}

    def fake_post(url, data=None, json=None, auth=None, timeout=None):
        seen.update(url=url, data=data)
        return _Resp(200, {"id": "pi_123", "status": "requires_payment_method"})
    monkeypatch.setattr(integrations.httpx, "post", fake_post)
    r = integrations.create_payment("abc123def456", 4200.0, "USD", "Freshworks")
    assert r["ok"] and r["id"] == "pi_123" and seen["data"]["amount"] == "420000"
    assert seen["data"]["metadata[case_id]"] == "abc123def456"


def test_razorpay_test_order(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "s")
    monkeypatch.setattr(integrations.httpx, "post",
                        lambda url, json=None, auth=None, timeout=None, data=None: _Resp(200, {"id": "order_9", "status": "created"}))
    r = integrations.create_payment("c1", 2100.0, "INR", "Zoho")
    assert r["ok"] and r["gateway"] == "razorpay"


def test_email_goes_to_safe_recipient(monkeypatch):
    for k, v in {"SMTP_HOST": "smtp.example", "SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw",
                 "SAFE_RECIPIENT": "safe@example.com"}.items():
        monkeypatch.setenv(k, v)
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): sent["login"] = u
        def send_message(self, msg): sent["to"] = msg["To"]; sent["subject"] = msg["Subject"]
    monkeypatch.setattr(integrations.smtplib, "SMTP_SSL", FakeSMTP)
    r = integrations.send_email("support@freshworks.com", "Please confirm", "body")
    assert r["ok"] and sent["to"] == "safe@example.com" and "support@freshworks.com" in sent["subject"]


def test_alert_posts_to_webhook(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/x")
    got = {}
    monkeypatch.setattr(integrations.httpx, "post",
                        lambda url, json=None, timeout=None: (got.update(url=url, json=json), _Resp(200, {}))[1])
    assert integrations.alert("BLOCK case 1") is None and got["json"]["text"] == "BLOCK case 1"


def test_imap_only_new_mail_and_never_marks_read(monkeypatch):
    for k, v in {"IMAP_HOST": "imap.example", "IMAP_USER": "ap@example.com", "IMAP_PASSWORD": "pw"}.items():
        monkeypatch.setenv(k, v)
    integrations._BASELINE.clear()
    msg = email.message.EmailMessage()
    msg["From"] = "Billing <billing@freshworks.com>"
    msg["Subject"] = "Invoice INV-1"
    msg["Message-ID"] = "<m1@x>"
    msg.set_content("Please pay USD 4,200.00 to our account on file.")
    box = {"uids": [b"1", b"2"]}
    calls = []

    class FakeIMAP:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def select(self, f, readonly=False): calls.append(("select", readonly)); return "OK", [b"2"]
        def uid(self, cmd, *args):
            calls.append((cmd, args))
            if cmd == "search":
                return "OK", [b" ".join(box["uids"])]
            return "OK", [(b"3", msg.as_bytes())]
    monkeypatch.setattr(integrations.imaplib, "IMAP4_SSL", FakeIMAP)
    assert integrations.fetch_unseen() == []                       # first poll: baseline only, old mail ignored
    box["uids"].append(b"3")                                       # a new email arrives
    items = integrations.fetch_unseen()
    assert len(items) == 1 and "USD 4,200.00" in items[0]["raw"]
    assert all(r is True for c, r in calls if c == "select")       # mailbox opened read-only
    assert any(c == "fetch" and "BODY.PEEK[]" in args[1] for c, args in calls)   # never marks as read
    assert integrations.fetch_unseen() == []                       # not returned twice


def test_juspay_sandbox_order(monkeypatch):
    monkeypatch.setenv("JUSPAY_API_KEY", "key")
    monkeypatch.setenv("JUSPAY_MERCHANT_ID", "payeeguard")
    seen = {}

    def fake_post(url, auth=None, timeout=None, headers=None, data=None, json=None):
        seen.update(url=url, headers=headers, data=data)
        return _Resp(200, {"order_id": data["order_id"], "status": "NEW", "payment_links": {"web": "https://sandbox/pay"}})
    monkeypatch.setattr(integrations.httpx, "post", fake_post)
    r = integrations.create_payment("abc123def456", 2100.0, "INR", "Zoho")
    assert r["ok"] and r["gateway"] == "juspay" and seen["url"] == "https://sandbox.juspay.in/orders"
    assert seen["headers"]["x-merchantid"] == "payeeguard" and seen["data"]["amount"] == "2100.00"


def test_juspay_production_host_refused(monkeypatch):
    monkeypatch.setenv("JUSPAY_API_KEY", "key")
    monkeypatch.setenv("JUSPAY_MERCHANT_ID", "m")
    monkeypatch.setenv("JUSPAY_BASE_URL", "https://api.juspay.in")
    r = integrations.create_payment("c1", 10, "INR", "V")
    assert not r["ok"] and "sandbox" in r["message"]
