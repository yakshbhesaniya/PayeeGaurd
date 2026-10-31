"""C15 relationship history: before paying, read OUR OWN conversations about this vendor.

Searches Slack (channels the PayeeGuard bot was added to) and the mailbox (inbox + sent mail) for the last 12 months,
only for messages that mention the vendor. If someone on our side ended the relationship ("we will not continue",
"terminate", "do not send invoices") and nobody re-engaged afterwards, a new invoice from that vendor is flagged with
the exact message cited. Nothing is stored except the cited snippet in the case evidence.
"""

import email
import email.policy
import imaplib
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import config
from .evidence import ev, unchecked

log = logging.getLogger(__name__)
LOOKBACK_DAYS = 365
CACHE_SECONDS = 60          # one search per vendor per minute, not per re-check
_CACHE: dict = {}

END_PHRASES = [
    "not continue", "won't continue", "will not continue", "wont continue", "not be continuing", "discontinue",
    "terminate", "terminating", "termination", "end our contract", "ending the contract", "end the contract",
    "cancel the contract", "cancelling the contract", "stop working with", "no longer work", "no longer working",
    "do not send", "don't send", "dont send", "stop sending", "offboard", "off-board", "not renew", "won't renew",
    "will not renew", "contract ended", "relationship ended", "part ways", "parting ways",
]
RESUME_PHRASES = ["renew", "renewed", "resume", "resuming", "continue working", "continuing with", "re-engage",
                  "reengage", "new contract", "extend the contract", "extended", "onboard again", "back on board"]


def _env(name: str) -> str:
    return config.env(name).strip()


def slack_enabled() -> bool:
    return _env("SLACK_BOT_TOKEN").startswith("xoxb-")


def mail_enabled() -> bool:
    return bool(_env("IMAP_HOST") and _env("IMAP_USER") and _env("IMAP_PASSWORD")) and _env("COMMS_MAIL_SEARCH") != "0"


def _terms(inv, ctx) -> list[str]:
    company = (ctx.get("company") or {}).get("basic_info") or {}
    names = [company.get("name"), inv.vendor_name]
    out = set()
    for n in names:
        if n:
            w = [x for x in re.findall(r"[A-Za-z0-9]+", n.lower())
                 if x not in {"inc", "ltd", "pvt", "private", "limited", "corp", "corporation", "llc", "the", "co"}]
            if w and len(w[0]) >= 3:
                out.add(w[0])
    for d in (company.get("primary_domain"), inv.vendor_domain):
        if d:
            out.add(d.lower())
    return sorted(out)


def _mentions(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low) for t in terms)


def _phrase(text: str, phrases: list[str]) -> str | None:
    low = text.lower()
    return next((p for p in phrases if p in low), None)


# ---- sources ------------------------------------------------------------------------------------------------

def search_slack(terms: list[str]) -> list[dict]:
    """Messages mentioning the vendor in channels the bot is a member of (needs channels:read, channels:history)."""
    token = _env("SLACK_BOT_TOKEN")
    if not token.startswith("xoxb-"):
        return []
    h = {"Authorization": f"Bearer {token}"}
    oldest = str(time.time() - LOOKBACK_DAYS * 86400)
    out = []
    with httpx.Client(timeout=10, headers=h) as c:
        channels = []
        for types in ("public_channel", "private_channel"):        # private needs groups:read; optional
            chans = c.get("https://slack.com/api/conversations.list",
                          params={"types": types, "exclude_archived": "true", "limit": 200}).json()
            if chans.get("ok"):
                channels += chans.get("channels", [])
            elif types == "public_channel":
                raise RuntimeError(f"Slack: {chans.get('error')}")
        for ch in channels:
            if not ch.get("is_member"):
                continue
            cursor = None
            for _ in range(5):                                   # up to ~1000 messages per channel
                params = {"channel": ch["id"], "oldest": oldest, "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                hist = c.get("https://slack.com/api/conversations.history", params=params).json()
                if not hist.get("ok"):
                    break
                for m in hist.get("messages", []):
                    text = m.get("text") or ""
                    if _mentions(text, terms):
                        when = datetime.fromtimestamp(float(m.get("ts", "0")), tz=timezone.utc)
                        out.append({"source": "slack", "where": f"#{ch.get('name')}", "when": when, "text": text,
                                    "cite": f"slack:#{ch.get('name')}@{m.get('ts')}"})
                cursor = (hist.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
    return out


def _mail_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    body = part.get_content()
    if part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)
    return body[:3000]


def search_mail(terms: list[str]) -> list[dict]:
    """Inbox + sent mail mentioning the vendor (server-side IMAP search, read-only, nothing marked as read)."""
    if not mail_enabled():
        return []
    since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - LOOKBACK_DAYS * 86400))
    out = []
    with imaplib.IMAP4_SSL(_env("IMAP_HOST"), int(_env("IMAP_PORT") or "993")) as im:
        im.login(_env("IMAP_USER"), _env("IMAP_PASSWORD"))
        for folder in ("INBOX", '"[Gmail]/Sent Mail"', "Sent"):
            try:
                typ, _ = im.select(folder, readonly=True)
                if typ != "OK":
                    continue
            except imaplib.IMAP4.error:
                continue
            uids = set()
            for t in terms:
                typ, data = im.uid("search", None, "SINCE", since, "TEXT", f'"{t}"')
                if typ == "OK" and data and data[0]:
                    uids |= set(data[0].split())
            for uid in sorted(uids, key=int)[-25:]:
                typ, parts = im.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                msg = email.message_from_bytes(parts[0][1], policy=email.policy.default)
                try:
                    when = parsedate_to_datetime(str(msg["Date"]))
                except (TypeError, ValueError):
                    continue
                text = f"{msg['Subject'] or ''}\n{_mail_text(msg)}"
                out.append({"source": "email", "where": f"{folder.strip(chr(34))}: {msg['Subject'] or '(no subject)'}",
                            "when": when, "text": text, "from": str(msg["From"] or ""),
                            "cite": f"mail:{folder.strip(chr(34))}#{uid.decode()} ({str(msg['Date'] or '')[:25]})"})
    return out


# ---- the check -----------------------------------------------------------------------------------------------

def check_c15_relationship(inv, ctx):
    claim = "We have not ended our relationship with this vendor"
    if not (slack_enabled() or mail_enabled()):
        return [unchecked("C15_relationship", "COMPANY", claim, "Slack/mail history not connected", "comms",
                          "not_applicable")]
    terms = _terms(inv, ctx)
    if not terms:
        return [unchecked("C15_relationship", "COMPANY", claim, "No vendor name to search for", "comms", "no_data")]
    key = tuple(terms)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        found, errors = hit[1], hit[2]
    else:
        found, errors = [], []
        for name, fn in (("Slack", search_slack), ("mail", search_mail)):
            try:
                found += fn(terms)
            except Exception as e:  # noqa: BLE001 - a broken source is unchecked, never fraud
                errors.append(f"{name}: {type(e).__name__}")
        _CACHE[key] = (time.time(), found, errors)
    sender = (inv.sender_email or "").lower()
    ends, resumes = [], []
    for m in found:
        if m["source"] == "email" and sender and sender in m.get("from", "").lower() and _phrase(m["text"], END_PHRASES) is None:
            continue                                  # the vendor's own invoice mails don't count as our decision
        p_end, p_res = _phrase(m["text"], END_PHRASES), _phrase(m["text"], RESUME_PHRASES)
        if p_end:
            ends.append((m, p_end))
        elif p_res:
            resumes.append((m, p_res))
    if ends:
        last_end, phrase = max(ends, key=lambda x: x[0]["when"])
        later = [r for r in resumes if r[0]["when"] > last_end["when"]]
        if not later:
            when = last_end["when"].strftime("%d %b %Y")
            snippet = re.sub(r"\s+", " ", last_end["text"]).strip()[:180]
            return [ev("C15_relationship", "COMPANY", claim, "contradicted",
                       f"On {when} in {last_end['where']} our side said \"{snippet}\" (matched \"{phrase}\"); no "
                       "renewal or re-engagement found since. Confirm whether this is a final invoice for work "
                       "delivered before that date; otherwise reject it.", last_end["cite"], 45)]
        r, _ = max(later, key=lambda x: x[0]["when"])
        return [ev("C15_relationship", "COMPANY", claim, "verified",
                   f"Relationship ended on {last_end['when'].strftime('%d %b %Y')} but was resumed on "
                   f"{r['when'].strftime('%d %b %Y')} ({r['where']})", r["cite"])]
    if errors and not found:
        return [unchecked("C15_relationship", "COMPANY", claim, "Could not read history: " + "; ".join(errors), "comms",
                          "api_error")]
    return [ev("C15_relationship", "COMPANY", claim, "verified",
               f"{len(found)} messages about {', '.join(terms)} in Slack/mail over 12 months; no termination found",
               "comms:slack+mail")]
