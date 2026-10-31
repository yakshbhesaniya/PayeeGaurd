"""Live integrations: real inbox (IMAP), real email (SMTP), payment gateway (test mode), Slack/Teams alerts.

Each one switches on only when its credentials are in .env; otherwise PayeeGuard stays in demo mode (drafts).
Safety rails, on purpose:
  * email goes to SAFE_RECIPIENT unless EMAIL_TO_VENDOR=1, so a demo never emails a real company's support team;
  * payments accept ONLY test keys (sk_test_ / rzp_test_): no real money can move from this code;
  * secrets are never logged or returned to the browser.
"""

import email
import email.policy
import html
import imaplib
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from . import config

log = logging.getLogger(__name__)


def _env(name: str) -> str:
    return config.env(name).strip()


# ---- status ---------------------------------------------------------------------------------------------

def status() -> dict:
    gw = gateway_name()
    return {
        "crustdata": bool(_env("CRUSTDATA_API_KEY")),
        "inbox": bool(_env("IMAP_HOST") and _env("IMAP_USER") and _env("IMAP_PASSWORD")),
        "inbox_user": _env("IMAP_USER") or None,
        "email": bool(_env("RESEND_API_KEY") or (_env("SMTP_HOST") and _env("SMTP_USER") and _env("SMTP_PASSWORD"))),
        "email_via": "resend (https)" if _env("RESEND_API_KEY") else ("smtp" if _env("SMTP_HOST") else None),
        "history": bool(_env("SLACK_BOT_TOKEN").startswith("xoxb-") or (_env("IMAP_HOST") and _env("IMAP_PASSWORD"))),
        "email_to": "vendor" if _env("EMAIL_TO_VENDOR") == "1" else (_env("SAFE_RECIPIENT") or _env("SMTP_USER") or None),
        "payments": gw is not None, "gateway": gw,
        "alerts": bool(_env("ALERT_WEBHOOK_URL")),
    }


# ---- Slack / Teams alerts ---------------------------------------------------------------------------------

def alert(text: str) -> str | None:
    """Post to a Slack or Teams incoming webhook. Returns an error string, or None on success / not configured."""
    url = _env("ALERT_WEBHOOK_URL")
    if not url:
        return None
    try:
        r = httpx.post(url, json={"text": text}, timeout=8)
        return None if r.status_code < 300 else f"alert webhook HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return f"alert webhook failed: {type(e).__name__}"


def verdict_alert(v, inv, base_url: str = "") -> str | None:
    if v.decision == "PAY":
        return None
    icon = ":octagonal_sign:" if v.decision == "BLOCK" else ":warning:"
    attack = v.hypothesis.title if v.hypothesis else "key facts unverified"
    amount = f"{inv.currency or ''} {inv.amount:,.2f}" if inv.amount is not None else "amount n/a"
    link = f" · {base_url}/#case-{v.case_id}" if base_url else ""
    return alert(f"{icon} PayeeGuard {v.decision}: {inv.vendor_name or 'unknown vendor'} · {amount} · {attack} "
                 f"(risk {v.risk_score}, {v.decision_rule}) · case {v.case_id}{link}")


# ---- SMTP: real email -------------------------------------------------------------------------------------

def send_email(intended_to: str | None, subject: str, body: str) -> dict:
    if not status()["email"]:
        return {"ok": False, "message": "Email not connected: add RESEND_API_KEY (HTTPS) or SMTP_* to .env"}
    to_vendor = _env("EMAIL_TO_VENDOR") == "1"
    to = intended_to if (to_vendor and intended_to and "@" in intended_to) else (_env("SAFE_RECIPIENT") or _env("SMTP_USER"))
    msg = EmailMessage()
    msg["From"] = _env("SMTP_FROM") or _env("SMTP_USER")
    msg["To"] = to
    msg["Subject"] = subject if to_vendor else f"[PayeeGuard → intended for {intended_to or 'vendor'}] {subject}"
    msg.set_content(body)
    if _env("RESEND_API_KEY"):
        # HTTPS email API: works on networks that block SMTP ports (465/587), e.g. venue Wi-Fi.
        try:
            r = httpx.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {_env('RESEND_API_KEY')}"},
                           json={"from": _env("RESEND_FROM") or "PayeeGuard <onboarding@resend.dev>", "to": [to],
                                 "subject": msg["Subject"], "text": body}, timeout=15)
            if r.status_code >= 300:
                return {"ok": False, "message": f"Email failed (Resend HTTP {r.status_code}): {r.text[:120]}"}
        except httpx.HTTPError as e:
            return {"ok": False, "message": f"Email failed: {type(e).__name__}"}
        note = "" if to_vendor else f" (safe mode: sent to {to} instead of {intended_to or 'the vendor'})"
        return {"ok": True, "message": f"Email sent via Resend to {to}{note}.", "to": to}
    try:
        port = int(_env("SMTP_PORT") or "465")
        if port == 465:
            with smtplib.SMTP_SSL(_env("SMTP_HOST"), port, context=ssl.create_default_context(), timeout=15) as s:
                s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
                s.send_message(msg)
        else:
            with smtplib.SMTP(_env("SMTP_HOST"), port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        return {"ok": False, "message": f"Email failed: {type(e).__name__}: {str(e)[:120]}"}
    note = "" if to_vendor else f" (safe mode: sent to {to} instead of {intended_to or 'the vendor'})"
    return {"ok": True, "message": f"Email sent to {to}{note}.", "to": to}


# ---- Payment gateway (TEST MODE ONLY) ---------------------------------------------------------------------

def _juspay_base() -> str:
    return (_env("JUSPAY_BASE_URL") or "https://sandbox.juspay.in").rstrip("/")


def gateway_name() -> str | None:
    if _env("JUSPAY_API_KEY") and _env("JUSPAY_MERCHANT_ID") and "sandbox" in _juspay_base():
        return "juspay"
    if _env("STRIPE_SECRET_KEY").startswith("sk_test_"):
        return "stripe"
    if _env("RAZORPAY_KEY_ID").startswith("rzp_test_") and _env("RAZORPAY_KEY_SECRET"):
        return "razorpay"
    return None


def create_payment(case_id: str, amount: float | None, currency: str | None, vendor: str | None) -> dict:
    """Create a real payment object in the gateway's TEST mode. Live keys are refused by design."""
    gw = gateway_name()
    if gw is None:
        if _env("STRIPE_SECRET_KEY") or _env("RAZORPAY_KEY_ID") or _env("JUSPAY_API_KEY"):
            return {"ok": False, "message": "Refused: only TEST/sandbox credentials are accepted "
                                            "(sk_test_ / rzp_test_ / a Juspay sandbox host)"}
        return {"ok": False, "message": "Payment gateway not connected (demo mode)"}
    if not amount or amount <= 0:
        return {"ok": False, "message": "No positive amount to pay"}
    cur = (currency or "USD").lower()
    minor = int(round(amount * 100))
    meta = {"case_id": case_id, "vendor": (vendor or "")[:80], "source": "payeeguard"}
    try:
        if gw == "juspay":
            order_id = f"pg-{case_id}-{minor}"
            r = httpx.post(f"{_juspay_base()}/orders", auth=(_env("JUSPAY_API_KEY"), ""), timeout=15,
                           headers={"x-merchantid": _env("JUSPAY_MERCHANT_ID"), "x-routing-id": case_id,
                                    "version": "2023-06-30"},
                           data={"order_id": order_id, "amount": f"{amount:.2f}", "currency": cur.upper(),
                                 "customer_id": "payeeguard-ap", "description": f"PayeeGuard case {case_id}: {vendor}",
                                 "udf1": case_id})
            j = r.json()
            if r.status_code >= 300:
                return {"ok": False, "message": f"Juspay: {j.get('error_message') or j.get('status') or r.status_code}"}
            link = (j.get("payment_links") or {}).get("web")
            return {"ok": True, "gateway": "juspay", "id": j.get("order_id", order_id), "status": j.get("status"),
                    "message": f"Juspay SANDBOX order {j.get('order_id', order_id)} created ({j.get('status')})",
                    "dashboard": link}
        if gw == "stripe":
            data = {"amount": str(minor), "currency": cur, "description": f"PayeeGuard case {case_id}: {vendor}",
                    "payment_method_types[]": "card", **{f"metadata[{k}]": v for k, v in meta.items()}}
            r = httpx.post("https://api.stripe.com/v1/payment_intents", data=data,
                           auth=(_env("STRIPE_SECRET_KEY"), ""), timeout=15)
            j = r.json()
            if r.status_code >= 300:
                return {"ok": False, "message": f"Stripe: {j.get('error', {}).get('message', r.status_code)}"}
            return {"ok": True, "gateway": "stripe", "id": j["id"], "status": j.get("status"),
                    "message": f"Stripe TEST payment {j['id']} created ({j.get('status')})",
                    "dashboard": f"https://dashboard.stripe.com/test/payments/{j['id']}"}
        r = httpx.post("https://api.razorpay.com/v1/orders", json={"amount": minor, "currency": cur.upper(),
                       "receipt": case_id, "notes": meta},
                       auth=(_env("RAZORPAY_KEY_ID"), _env("RAZORPAY_KEY_SECRET")), timeout=15)
        j = r.json()
        if r.status_code >= 300:
            return {"ok": False, "message": f"Razorpay: {j.get('error', {}).get('description', r.status_code)}"}
        return {"ok": True, "gateway": "razorpay", "id": j["id"], "status": j.get("status"),
                "message": f"Razorpay TEST order {j['id']} created ({j.get('status')})",
                "dashboard": "https://dashboard.razorpay.com/app/orders"}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return {"ok": False, "message": f"Gateway error: {type(e).__name__}"}


# ---- IMAP: real inbox -------------------------------------------------------------------------------------

def _to_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    body = part.get_content()
    if part.get_content_type() == "text/html":
        body = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", body)))
    return body


_BASELINE: list[int] = []     # highest mailbox UID when the watcher started: older mail is never read


def fetch_unseen(limit: int = 10) -> list[dict]:
    """Return NEW messages (arrived after the watcher started) as raw-text emails ready for verify().

    Privacy for personal mailboxes: mail that was already there is never fetched, nothing is marked as read
    (BODY.PEEK), and the caller drops messages that aren't payment requests.
    """
    if not status()["inbox"]:
        return []
    out = []
    with imaplib.IMAP4_SSL(_env("IMAP_HOST"), int(_env("IMAP_PORT") or "993")) as im:
        im.login(_env("IMAP_USER"), _env("IMAP_PASSWORD"))
        im.select(_env("IMAP_FOLDER") or "INBOX", readonly=True)
        typ, data = im.uid("search", None, "ALL")
        uids = [int(u) for u in (data[0].split() if typ == "OK" and data and data[0] else [])]
        if not _BASELINE:
            _BASELINE.append(max(uids) if uids else 0)
            return []                                   # first poll only records where "new" starts
        new = [u for u in uids if u > _BASELINE[0]][-limit:]
        for uid in new:
            typ, parts = im.uid("fetch", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            _BASELINE[0] = max(_BASELINE[0], uid)
            mid = str(uid).encode()
            msg = email.message_from_bytes(parts[0][1], policy=email.policy.default)
            headers = [f"{h}: {msg[h]}" for h in ("From", "Reply-To", "Return-Path", "Subject", "Date") if msg[h]]
            raw = "\n".join(headers) + "\n\n" + _to_text(msg)
            out.append({"message_id": str(msg["Message-ID"] or mid.decode()), "subject": str(msg["Subject"] or ""),
                        "from": str(msg["From"] or ""), "date": str(msg["Date"] or ""), "raw": raw[:20_000]})
    return out
