"""Deterministic extractor: raw email text -> Invoice (+ ExtractMeta). No LLM needed.

The MCP server always re-runs this on raw_text; for security-critical fields the regex value wins over
whatever Claude passed, and disagreements become X1 evidence (spec 7, edge cases M2/E1-E16).
"""

import hashlib
import html
import re
from dataclasses import dataclass, field

from . import config, geo
from .models import Evidence, Invoice

MAX_CHARS = 20_000
FIELD_MAX = 200

FORWARD_MARKERS = re.compile(
    r"^-{2,}\s*Forwarded message\s*-{2,}|^Begin forwarded message:|^-{2,}\s*Original Message\s*-{2,}",
    re.IGNORECASE | re.MULTILINE)
QUOTE_INTRO = re.compile(r"^On .{3,120}wrote:\s*$", re.MULTILINE)
ROLE_WORDS = {"billing", "accounts", "account", "team", "support", "noreply", "no-reply", "finance",
              "payments", "receivable", "payable", "invoice", "invoices", "ar", "ap", "collections", "admin"}
ROLE_LOCALPARTS = {"billing", "accounts", "ar", "ap", "invoices", "invoice", "finance", "payments",
                   "noreply", "no-reply", "support", "collections", "receivables"}
CURRENCY_SYMBOLS = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP", "rs": "INR", "rs.": "INR", "inr": "INR"}
CURRENCY_CODES = "USD|INR|EUR|GBP|SGD|AED|AUD|CAD|JPY|CHF"
BANK_CHANGE = re.compile(
    r"new (bank|account|remittance)|updated? (bank|account|remittance|\w+ account|details)|change (of|in) bank"
    r"|bank details (have )?changed|(account|bank) (is )?(under audit|frozen|under maintenance|closed)"
    r"|remit (this invoice )?to our new|new bank details|update (our|the) bank", re.IGNORECASE)
ON_FILE = re.compile(r"(existing account|account on file|usual account|same account)", re.IGNORECASE)
TITLE_WORDS = re.compile(
    r"(chief [a-z ]+officer|cfo|ceo|controller|treasurer|[a-z ]*(manager|executive|specialist|analyst|lead|head|"
    r"director|officer|associate|accountant|clerk)[a-z ]*|accounts? (receivable|payable)|accounts)", re.IGNORECASE)


@dataclass
class ExtractMeta:
    forwarded: bool = False
    display_name: str | None = None
    accounts: list[dict] = field(default_factory=list)   # distinct payment destinations found
    amounts: list[tuple[float, str, bool]] = field(default_factory=list)  # (value, currency, labelled_total)
    amount_conflict: bool = False
    credit_note: bool = False
    not_payment: bool = False
    empty: bool = False
    stated_country: str | None = None   # country named in the text (vs IBAN/SWIFT country)
    on_file: bool = False
    currency_assumed: bool = False


def case_id_for(raw_text: str) -> str:
    norm = re.sub(r"\s+", " ", raw_text).strip().lower()
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def _clean(raw: str) -> str:
    t = raw[:MAX_CHARS]
    if re.search(r"<(html|body|div|p|br|table)\b", t, re.IGNORECASE):
        t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", t, flags=re.IGNORECASE)
        t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(t).replace("\r\n", "\n")


def normalise_email(e: str | None) -> str | None:
    if not e:
        return None
    e = e.strip().strip("<>").lower()
    e = re.sub(r"^mailto:", "", e)
    e = re.sub(r"\s*\[\s*at\s*\]\s*|\s+at\s+", "@", e)
    e = re.sub(r"\s*\[\s*dot\s*\]\s*", ".", e)
    if "@" not in e:
        return None
    local, dom = e.rsplit("@", 1)
    local = local.split("+", 1)[0]
    dom = dom.strip(".")
    return f"{local}@{dom}" if local and "." in dom else None


EMAIL_RE = r"[\w.+'-]+\s*(?:@|\[\s*at\s*\])\s*[\w-]+(?:\s*(?:\.|\[\s*dot\s*\])\s*[\w-]+)+"


def _parse_address(value: str) -> tuple[str | None, str | None]:
    """'Name <a@b.com>' -> (name, email); also handles bare emails and quoted names."""
    value = value.strip()
    m = re.search(r"<\s*(" + EMAIL_RE + r")\s*>", value)
    if m:
        name = value[: m.start()].strip().strip('"').strip("'").strip() or None
        return name, normalise_email(m.group(1))
    m = re.search(EMAIL_RE, value)
    return None, normalise_email(m.group(0)) if m else None


def _header(block: str, name: str) -> str | None:
    m = re.search(rf"^{name}:\s*(.+)$", block, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else None


def _sender_block(text: str) -> tuple[str, bool]:
    """Edge case E3: in a forward, the real sender is inside the most recent forwarded block."""
    markers = list(FORWARD_MARKERS.finditer(text))
    buyer = config.policy()["buyer"].get("buyer_domain") or ""
    if markers:
        return text[markers[-1].end():], True
    top_from = _header(text, "From") or ""
    if buyer and buyer.lower() in top_from.lower():
        return text, True
    return text, False


def _strip_quotes(text: str) -> str:
    """Edge case E4: drop '>' quoted lines and everything after an 'On ... wrote:' intro."""
    m = QUOTE_INTRO.search(text)
    if m:
        text = text[: m.start()]
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(">"))


def parse_number(s: str) -> float | None:
    s = s.strip().replace(" ", " ")
    mult = 1.0
    low = s.lower()
    if "lakh" in low or "lac" in low:
        mult, s = 100_000.0, re.sub(r"(?i)\s*(lakhs?|lacs?)", "", s)
    elif "crore" in low:
        mult, s = 10_000_000.0, re.sub(r"(?i)\s*crores?", "", s)
    s = s.rstrip("/-").strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{1,2}", s) or re.fullmatch(r"\d{1,3}( \d{3})+,\d{1,2}", s):
        s = s.replace(".", "").replace(" ", "").replace(",", ".")          # 9.800,00 / 9 800,00
    elif re.fullmatch(r"\d+,\d{1,2}", s):
        s = s.replace(",", ".")                                             # 49,50
    else:
        s = s.replace(",", "").replace(" ", "")                              # 4,200.00 / 3,45,000
    try:
        return float(s) * mult
    except ValueError:
        return None


NUM = r"\d[\d,.  ]*\d(?:\s*(?:lakhs?|lacs?|crores?))?|\d"
MONEY_RE = re.compile(
    rf"(?P<pre>{CURRENCY_CODES}|Rs\.?|₹|\$|€|£)\s?(?P<n1>{NUM})(?:\s*/-)?"
    rf"|(?P<n2>{NUM})\s?(?P<post>{CURRENCY_CODES}|€|£)(?![A-Za-z])", re.IGNORECASE)


def _amounts(text: str) -> tuple[list[tuple[float, str, bool]], bool]:
    found, assumed = [], False
    for m in MONEY_RE.finditer(text):
        cur_raw = (m.group("pre") or m.group("post") or "").strip()
        num = m.group("n1") or m.group("n2")
        val = parse_number(num or "")
        if val is None:
            continue
        cur = cur_raw.upper()
        if cur_raw.lower() in CURRENCY_SYMBOLS:
            cur = CURRENCY_SYMBOLS[cur_raw.lower()]
            assumed = assumed or cur_raw == "$"
        elif cur_raw in CURRENCY_SYMBOLS:
            cur = CURRENCY_SYMBOLS[cur_raw]
            assumed = assumed or cur_raw == "$"
        window = text[max(0, m.start() - 40): m.start()].lower()
        labelled = bool(re.search(r"total|amount due|balance due|invoice[^\n]{0,30}for|pay\b|remit", window))
        found.append((round(val, 2), cur, labelled))
    return found, assumed


def _bank(text: str) -> tuple[list[dict], dict]:
    """Return distinct accounts found and the primary one's fields."""
    accounts: list[dict] = []
    for m in re.finditer(r"IBAN[:\s]*([A-Z]{2}\d{2}(?: ?[A-Z0-9]{1,4}){3,8})", text, re.IGNORECASE):
        s = geo.iban_normalise(m.group(1))
        L = geo.IBAN_LENGTHS.get(s[:2])
        if L and len(s) > L:
            s = s[:L]
        accounts.append({"iban": s})
    swifts = [s.upper() for s in re.findall(
        r"(?:SWIFT|BIC)(?:\s*code)?[:\s]*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", text, re.IGNORECASE)]
    ifscs = [s.upper() for s in re.findall(r"IFSC(?:\s*code)?[:\s]*([A-Z]{4}0[A-Z0-9]{6})", text, re.IGNORECASE)]
    for m in re.finditer(r"(?:account|a/c|acct)(?:\s*(?:no\.?|number|#))?[:\s]+(\d[\d -]{5,24}\d)", text,
                         re.IGNORECASE):
        accounts.append({"account": re.sub(r"\D", "", m.group(1))})
    last4 = None
    m = re.search(r"(?:account|a/c)\s*(?:ending|ending in|ends with|no\. ending)\s*(?:in\s*)?[:\s]*(\d{4})\b", text,
                  re.IGNORECASE)
    if m:
        last4 = m.group(1)
    code = (swifts or ifscs or [None])[0]
    for a in accounts:
        if "account" in a and code:
            a["code"] = code
    # De-duplicate (same IBAN/account mentioned twice is one destination).
    seen, distinct = set(), []
    for a in accounts:
        k = a.get("iban") or a.get("account")
        if k not in seen:
            seen.add(k)
            distinct.append(a)
    primary = distinct[0] if distinct else {}
    return distinct, {"iban": primary.get("iban"), "account": primary.get("account"), "code": code,
                      "last4": last4 or ((primary.get("iban") or primary.get("account") or "")[-4:] or None)}


def _title_near(text: str, name: str | None) -> str | None:
    if name:
        m = re.search(re.escape(name) + r"\s*,\s*([^,\n]{3,60})", text)
        if m and TITLE_WORDS.search(m.group(1)):
            return m.group(1).strip()
    m = re.search(r"\bI am (?:the |a |an )?([A-Za-z ]{3,50}?)\s+(?:at|of|with)\s", text)
    if m and TITLE_WORDS.search(m.group(1)):
        return m.group(1).strip()
    return None


def _vendor(text: str, display: str | None, subject: str | None) -> tuple[str | None, str | None]:
    m = re.search(r"^Vendor:\s*([^\n,]+?)(?:\s*,\s*([\w.-]+\.[a-z]{2,}))?\s*$", text, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1).strip(), (m.group(2) or None)
    m = re.search(r"\b(?:at|from)\s+([A-Z][\w&.\- ]{1,40}?)\s*\(([\w.-]+\.[a-z]{2,})\)", text)
    if m:
        return m.group(1).strip(), m.group(2).lower()
    if display:
        words = [w for w in re.split(r"\s+", display) if w.lower().strip(",.") not in ROLE_WORDS]
        if words and len(words) < len(display.split()):
            return " ".join(words), None
    if subject:
        m = re.match(r"\s*([A-Z][\w&.-]+(?: [A-Z][\w&.-]+)?)\s+(?:invoice|bill)\b", subject)
        if m:
            return m.group(1), None
    return None, None


def _is_role_sender(display: str | None, email: str | None) -> bool:
    local = (email or "").split("@")[0]
    if local in ROLE_LOCALPARTS:
        return display is None or any(w.lower() in ROLE_WORDS for w in display.split())
    return bool(display) and any(w.lower().strip(",.") in ROLE_WORDS for w in display.split())


def extract(raw_text: str) -> tuple[Invoice, ExtractMeta]:
    meta = ExtractMeta()
    text = _clean(raw_text or "")
    if not text.strip():
        meta.empty = True
        return Invoice(None, None, None, None, None, None, None, None, None, raw_text=raw_text or ""), meta

    block, meta.forwarded = _sender_block(text)
    body = _strip_quotes(block)
    display, sender = _parse_address(_header(block, "From") or "")
    meta.display_name = display
    _, reply_to = _parse_address(_header(block, "Reply-To") or "")
    _, return_path = _parse_address(_header(block, "Return-Path") or "")
    subject = _header(block, "Subject")

    vendor_name, vendor_domain = _vendor(body, display, subject)
    sender_name = None if _is_role_sender(display, sender) else display
    accounts, bank = _bank(body)
    meta.accounts = accounts
    meta.amounts, meta.currency_assumed = _amounts(body)

    amount = currency = None
    if meta.amounts:
        labelled = [a for a in meta.amounts if a[2]] or meta.amounts
        amount, currency = labelled[0][0], labelled[0][1]
        totals = {(a[0], a[1]) for a in meta.amounts if a[2]}
        meta.amount_conflict = len(totals) > 1
    low = body.lower()
    meta.credit_note = "credit note" in low or (amount is not None and amount <= 0)
    meta.on_file = bool(ON_FILE.search(body))
    bank_lines = "\n".join(line for line in body.split("\n")
                           if re.search(r"bank|account|remit|pay|iban|swift|ifsc|entity", line, re.IGNORECASE))
    meta.stated_country = geo.find_country(bank_lines) if bank_lines else None
    code = bank["code"]
    remit = (geo.iban_normalise(bank["iban"])[:2] if bank["iban"] else None) \
        or ("IN" if geo.is_ifsc(code) else geo.swift_country(code) if code else None) or meta.stated_country
    urgency = any(p in low for p in config.policy()["tactics"].get("urgency", []))
    inv_no = None
    m = re.search(r"\b([A-Z]{2,5}-\d{4}-\d{3,5}|[A-Z]{2,5}-\d{4,6})\b", (subject or "") + "\n" + body)
    if m:
        inv_no = m.group(1)
    if not meta.amounts and not inv_no and not accounts and not re.search(r"invoice|payment|remit|bank", low):
        meta.not_payment = True

    inv = Invoice(
        vendor_name=_trim(vendor_name), vendor_domain=_trim(vendor_domain), sender_email=sender,
        sender_name=_trim(sender_name), sender_title=_trim(_title_near(body, sender_name or display)),
        remit_country=remit, vendor_address=None, amount=amount, currency=currency,
        bank_change_requested=bool(BANK_CHANGE.search(body)), urgency_language=urgency,
        raw_text=raw_text, invoice_number=inv_no, reply_to_email=reply_to, return_path_email=return_path,
        bank_iban=bank["iban"], bank_account=bank["account"], bank_swift=code, bank_account_last4=bank["last4"])
    return inv, meta


def _trim(v: str | None) -> str | None:
    return v[:FIELD_MAX] if isinstance(v, str) else v


CRITICAL = ("sender_email", "reply_to_email", "bank_iban", "bank_account", "bank_swift", "amount")


def cross_check(inv: Invoice, llm_fields: dict | None) -> tuple[Invoice, list[Evidence]]:
    """Merge Claude-supplied fields: regex wins for critical fields; mismatches become X1 evidence."""
    if not llm_fields:
        return inv, []
    evidence = []
    for key, val in llm_fields.items():
        if val in (None, "") or not hasattr(inv, key) or key == "raw_text":
            continue
        val = _trim(val) if isinstance(val, str) else val
        mine = getattr(inv, key)
        if key in CRITICAL:
            a = _norm_field(key, mine)
            b = _norm_field(key, val)
            if a is not None and b is not None and a != b:
                evidence.append(Evidence(
                    "X1_extraction", "INTEGRITY", f"Claude's {key} matches the email text", "contradicted",
                    f"Claude passed {key}={_mask(key, val)!s} but the email text says {_mask(key, mine)!s}; "
                    "the text value is used", f"extract:regex#{key} vs extract:llm#{key}", 10))
            elif a is None:
                # Grounding rule: an LLM may only POINT AT text, never invent it. A critical value the regex missed
                # is accepted only if it literally appears in the email; otherwise it is ignored and reported.
                if grounded(key, val, inv.raw_text):
                    setattr(inv, key, val)
                else:
                    evidence.append(Evidence(
                        "X1_extraction", "INTEGRITY", f"Claude's {key} appears in the email text", "contradicted",
                        f"Claude passed {key}={_mask(key, val)!s}, which does not appear anywhere in the email; ignored",
                        f"extract:llm#{key} not grounded in raw_text", 10))
        elif mine in (None, "", False):
            setattr(inv, key, val)
    return inv, evidence


def grounded(key: str, value, raw_text: str) -> bool:
    """True if a (normalised) LLM-extracted value is present in the raw email text."""
    raw = raw_text or ""
    if key in ("sender_email", "reply_to_email"):
        e = normalise_email(str(value))
        if not e:
            return False
        local, dom = e.split("@")
        low = raw.lower()
        return dom in low and local in low
    if key == "amount":
        try:
            target = round(float(value), 2)
        except (TypeError, ValueError):
            return False
        nums = [parse_number(m.group(0)) for m in re.finditer(r"\d[\d,.  ]*\d(?:\s*(?:lakhs?|lacs?|crores?))?|\d", raw)]
        return any(n is not None and abs(n - target) < 0.01 for n in nums)
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    v = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    return len(v) >= 4 and v in compact


def _norm_field(key: str, v):
    if v is None or v == "":
        return None
    if key in ("sender_email", "reply_to_email"):
        return normalise_email(str(v))
    if key == "amount":
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None
    return re.sub(r"[^A-Za-z0-9]", "", str(v)).upper()


def _mask(key: str, v) -> str:
    if key in ("bank_iban", "bank_account") and v:
        s = re.sub(r"[^A-Za-z0-9]", "", str(v))
        return f"••••{s[-4:]}"
    return str(v)
