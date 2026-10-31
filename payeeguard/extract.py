"""Regex extractor: raw email text -> Invoice, plus X1/X2/G5 integrity evidence (spec 7, 13.1).

The regex is the authority for critical fields; values Claude passes in are only cross-checked (X1).
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field, fields, replace
from typing import Any, Optional

from .config import policy
from .models import Evidence, Invoice

MAX_TEXT = 20_000   # E15
MAX_FIELD = 200     # E16

FORWARD_MARKERS = re.compile(
    r"^\s*(?:-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message:?|-{2,}\s*Original Message\s*-{2,})\s*$",
    re.I | re.M)
REPLY_HEADER = re.compile(r"^\s*On\b.{0,200}\bwrote:", re.I | re.M)
HEADER = re.compile(r"^\s*(From|Reply-To|Return-Path|Subject|Date|To|Sent)\s*:\s*(.*)$", re.I | re.M)
EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)

CURRENCY_CODES = ("USD", "EUR", "GBP", "INR", "AED", "SGD", "AUD", "CAD", "CHF", "JPY")
SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "RS": "INR", "RS.": "INR"}
# space thousands only in groups of 3 ("9 800,00"), so "4200 2026" never merges into one number
NUM = r"\d{1,3}(?:[ \u00a0\u202f]\d{3}){1,6}(?:[.,]\d{1,2})?|\d[\d,.]{0,22}\d|\d"   # bounded: no backtracking blow-up
SCALE = r"(?:\s*(?P<scale>lakhs?|lacs?|crores?|cr|million|mn)\b)?"
AMOUNT_PATTERNS = [
    # USD 4,200.00 · INR 3.45 lakh · Rs. 3,45,000/- · $4,200 · ₹3,45,000 · -USD 500
    re.compile(r"(?P<neg>-\s*)?(?P<cur>\b(?:%s)\b|US\$|\$|€|£|₹|\bRs\.?)\s*(?P<neg2>-\s*)?(?P<num>%s)%s"
               % ("|".join(CURRENCY_CODES), NUM, SCALE), re.I),
    # 9 800,00 € · 9.800,00 EUR
    re.compile(r"(?P<neg>-\s*)?(?P<num>%s)\s*(?P<cur>€|£|\b(?:%s)\b)" % (NUM, "|".join(CURRENCY_CODES)), re.I),
]
SCALES = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7,
          "k": 1e3, "million": 1e6, "mn": 1e6}
TOTAL_LABEL = re.compile(r"\b(total|amount due|balance due|grand total|invoice total|amount payable)\b", re.I)

IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,32})\b", re.I)
IBAN_LEN = {"AD": 24, "AE": 23, "AT": 20, "BE": 16, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18,
            "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IT": 27,
            "LU": 20, "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "SA": 24, "SE": 24, "TR": 26}
IBAN_COUNTRIES = set(IBAN_LEN) | {
    "AL", "AZ", "BA", "BG", "BH", "BR", "BY", "CR", "DO", "EE", "EG", "FO", "GE", "GI", "GL", "GT", "IL", "IQ",
    "IS", "JO", "KW", "KZ", "LB", "LC", "LI", "LT", "LV", "MC", "MD", "ME", "MK", "MR", "MU", "PK", "PS", "QA",
    "RO", "RS", "SC", "SI", "SK", "SM", "ST", "SV", "TL", "TN", "UA", "VA", "VG", "XK"}
SWIFT = re.compile(r"\b(?:SWIFT|BIC)(?:\s*/\s*BIC)?(?:\s*code)?\s*[:#]?\s*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b",
                   re.I)
IFSC = re.compile(r"\bIFSC(?:\s*code)?\s*[:#]?\s*([A-Z]{4}0[A-Z0-9]{6})\b", re.I)
ACCOUNT = re.compile(r"\b(?:account|a/c|acct)(?:\s*(?:number|no\.?|num|#))?\s*[:#]?\s*(\d[\d \-]{4,24}\d)\b", re.I)
INVOICE_NO = re.compile(
    r"(?i:\binvoice|\binv\b\.?)(?:\s*(?i:no\.?|number|num|#))?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]*\d[A-Z0-9\-/]*)")
VENDOR_LINE = re.compile(r"^\s*(?:Vendor|Supplier|Payee|Company)\s*:\s*(.+)$", re.I | re.M)
VENDOR_INLINE = re.compile(r"\b(?:at|from|of)\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,4})\s*\(([a-z0-9.\-]+\.[a-z]{2,})\)")
ADDRESS = re.compile(r"^\s*(?:Address|Registered office)\s*:\s*(.+)$", re.I | re.M)
DOMAIN = re.compile(r"\b((?:[a-z0-9\-]+\.)+[a-z]{2,})\b", re.I)

BANK_CHANGE = re.compile(
    r"\b(new|updated|changed?|different|revised|alternate)\s+(?:\w+\s+)?(bank|account|remittance|banking)\b"
    r"|\bbank (details|account) (have|has) (been )?changed\b|\bchange (of|in) bank\b"
    r"|\baccount (is )?(under audit|frozen|under maintenance|closed|blocked)\b", re.I)
ACCOUNT_ON_FILE = re.compile(r"\b(account on file|existing account|usual account|same account|registered account)\b", re.I)
CREDIT_NOTE = re.compile(r"\bcredit note\b|\bcredit memo\b", re.I)

COUNTRIES = {
    "united states": "US", "usa": "US", "u.s.": "US", "america": "US", "united kingdom": "GB", "uk": "GB",
    "britain": "GB", "england": "GB", "india": "IN", "nigeria": "NG", "germany": "DE", "france": "FR",
    "netherlands": "NL", "ireland": "IE", "spain": "ES", "italy": "IT", "singapore": "SG", "uae": "AE",
    "united arab emirates": "AE", "dubai": "AE", "hong kong": "HK", "china": "CN", "canada": "CA",
    "australia": "AU", "switzerland": "CH", "japan": "JP", "lagos": "NG",
}
COUNTRY_RE = re.compile(r"\b(%s)\b" % "|".join(sorted(map(re.escape, COUNTRIES), key=len, reverse=True)), re.I)
MONEY_LINE = re.compile(r"\b(account|bank|remit|iban|swift|ifsc|pay(?:ment)?\s+to|transfer)\b", re.I)

CRITICAL = ("sender_email", "reply_to_email", "bank_iban", "bank_account", "bank_swift", "amount")
GROUNDED = ("vendor_domain",)   # steers which company C1 resolves: accepted only if it appears in the text


@dataclass
class Extraction:
    invoice: Invoice
    evidence: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)


# ---------- normalisation ----------

def strip_html(text: str) -> str:
    """E5: drop tags (keeping line structure) and decode entities."""
    if not re.search(r"<\s*(html|body|div|p|br|span|table|td|tr|a|b|strong)\b", text, re.I):
        return html.unescape(text)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def normalise_text(raw: str) -> str:
    text = strip_html(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    return text[:MAX_TEXT]


def case_id_for(raw: str) -> str:
    """sha256 of the *full* normalised text (E15: hashed before truncation)."""
    norm = re.sub(r"\s+", " ", strip_html(raw or "")).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def normalise_email(value: Optional[str]) -> Optional[str]:
    """E13: lowercase, strip mailto:/+tag/trailing dot, de-obfuscate [at] and [dot]."""
    if not value:
        return None
    s = value.strip().lower()
    s = re.sub(r"^mailto:", "", s)
    s = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*|\s+at\s+(?=[a-z0-9\-]+\s*(?:\.|[\[(]\s*dot))", "@", s)
    s = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", s)
    m = EMAIL.search(s.replace(" ", "")) if "@" in s else None
    if not m:
        return None
    local, _, domain = m.group(0).partition("@")
    local = local.split("+", 1)[0]
    domain = domain.rstrip(".")
    return f"{local}@{domain}" if local and domain else None


def normalise_iban(v: Optional[str]) -> Optional[str]:
    return re.sub(r"[\s\-]", "", v).upper() if v else None


def normalise_account(v: Optional[str]) -> Optional[str]:
    return re.sub(r"[\s\-]", "", v) if v else None


def iban_valid(iban: str) -> bool:
    s = normalise_iban(iban) or ""
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def mask_account(inv: Invoice) -> str:
    """Y2: only country + last 4 ever leave memory."""
    last4 = inv.bank_account_last4 or "????"
    return f"{inv.remit_country or '??'} ••{last4}"


# ---------- header / sender ----------

def _parse_address(value: str) -> tuple[Optional[str], Optional[str]]:
    """'Name <a@b.com>' -> (name, email). Display names that are themselves emails are kept as names (H4)."""
    value = value.strip()
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>', value)
    if m:
        return (m.group(1).strip() or None), normalise_email(m.group(2))
    return None, normalise_email(value)


def _message_block(text: str) -> tuple[str, bool]:
    """E3: the most recent forwarded block is the real message; returns (block, was_forwarded)."""
    m = FORWARD_MARKERS.search(text)
    if m:
        return text[m.end():], True
    buyer = (policy()["buyer"].get("buyer_domain") or "").lower().strip()
    if buyer:
        froms = [h for h in HEADER.finditer(text) if h.group(1).lower() == "from"]
        if len(froms) >= 2:
            _, top = _parse_address(froms[0].group(2))
            if top and top.split("@")[1].endswith(buyer):
                return text[froms[1].start():], True
    return text, False


def _strip_quoted(body: str) -> str:
    """E4: drop '>' lines and everything after an 'On ... wrote:' reply header."""
    m = REPLY_HEADER.search(body)
    if m:
        body = body[:m.start()]
    return "\n".join(line for line in body.split("\n") if not line.lstrip().startswith(">"))


def _headers(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in HEADER.finditer(block):
        out.setdefault(h.group(1).lower(), h.group(2).strip())
    return out


# ---------- amounts ----------

def parse_number(s: str, currency: Optional[str] = None) -> Optional[float]:
    s = re.sub(r"[\s\u00a0\u202f]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(",", ".")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", "") if len(parts[-1]) == 3 else "".join(parts[:-1]) + "." + parts[-1]
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif "." in s and len(s.split(".")[-1]) == 3 and currency == "EUR":
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class _Amount:
    value: float
    currency: str
    labelled: bool
    assumed: bool
    pos: int


def _line_amounts(line: str, labelled: bool) -> list[_Amount]:
    out: list[_Amount] = []
    taken: list[tuple[int, int]] = []
    for pat in AMOUNT_PATTERNS:
        for m in pat.finditer(line):
            if any(m.start() < b and a < m.end() for a, b in taken):
                continue
            raw_cur = m.group("cur").upper()
            cur = SYMBOLS.get(raw_cur, raw_cur if raw_cur in CURRENCY_CODES else None)
            value = parse_number(m.group("num"), cur) if cur else None
            if value is None:
                continue
            scale = (m.groupdict().get("scale") or "").lower()
            value *= SCALES.get(scale, 1)
            if m.group("neg") or m.groupdict().get("neg2"):
                value = -value
            taken.append((m.start(), m.end()))
            out.append(_Amount(value, cur, labelled, raw_cur == "$", m.start()))
    return sorted(out, key=lambda a: a.pos)


def find_amounts(text: str, subject: str = "") -> list[_Amount]:
    """Subject amounts count as labelled totals (E8: subject vs body)."""
    found = _line_amounts(subject, True) if subject else []
    for line in text.split("\n"):
        found += _line_amounts(line, bool(TOTAL_LABEL.search(line)))
    return found


# ---------- bank details ----------

def _best_iban(candidate: str) -> str:
    """The IBAN regex can run into the next word ('... 00 SWIFT'); keep the longest checksum-valid prefix."""
    tokens = candidate.split()
    options = [" ".join(tokens[:i]) for i in range(len(tokens), 0, -1)]
    for opt in options:
        if len(normalise_iban(opt) or "") >= 15 and iban_valid(opt):
            return opt
    want = IBAN_LEN.get(candidate[:2].upper())
    for opt in options:
        if want and len(normalise_iban(opt) or "") == want:
            return opt
    while len(tokens) > 1 and tokens[-1].isalpha():
        tokens.pop()
    return " ".join(tokens)


def find_bank(text: str) -> dict[str, list[str]]:
    ibans = []
    for m in IBAN.finditer(text):
        if m.group(1)[:2].upper() not in IBAN_COUNTRIES:
            continue
        iban = normalise_iban(_best_iban(m.group(1)))
        if iban and len(iban) >= 15 and not iban[4:].isalpha() and iban not in ibans:
            ibans.append(iban)
    accounts = []
    for m in ACCOUNT.finditer(text):
        acc = normalise_account(m.group(1))
        if acc and len(acc) >= 6 and acc not in accounts and not any(acc in i for i in ibans):
            accounts.append(acc)
    swifts = list(dict.fromkeys(m.group(1).upper() for m in SWIFT.finditer(text)))
    ifscs = list(dict.fromkeys(m.group(1).upper() for m in IFSC.finditer(text)))
    return {"iban": ibans, "account": accounts, "swift": swifts, "ifsc": ifscs}


def _country_from_text(text: str) -> Optional[str]:
    for line in text.split("\n"):
        if MONEY_LINE.search(line):
            m = COUNTRY_RE.search(line)
            if m:
                return COUNTRIES[m.group(1).lower()]
    return None


def remit_country(stated: Optional[str], iban: Optional[str], swift: Optional[str], ifsc: Optional[str]) -> Optional[str]:
    """Stated -> IBAN prefix -> SWIFT chars 5-6 -> IFSC (IN)."""
    if stated:
        return stated
    if iban:
        return iban[:2].upper()
    if swift and len(swift) >= 6:
        return swift[4:6].upper()
    return "IN" if ifsc else None


# ---------- vendor / signatory ----------

def _clip(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip().strip(".,;")
    return v[:MAX_FIELD] or None


def _vendor(body: str) -> tuple[Optional[str], Optional[str]]:
    m = VENDOR_LINE.search(body)
    if m:
        line = m.group(1).strip()
        dm = DOMAIN.search(line)
        name = line[:dm.start()].strip(" ,;-(") if dm else line
        return _clip(name), dm.group(1).lower() if dm else None
    m = VENDOR_INLINE.search(body)
    if m:
        return _clip(m.group(1)), m.group(2).lower()
    return None, None


def _title(body: str, name: Optional[str]) -> Optional[str]:
    if name:
        m = re.search(re.escape(name) + r"\s*,\s*([A-Z][A-Za-z &/\-]{2,60}?)\s*(?:,|\n|$)", body)
        if m:
            return _clip(m.group(1))
    m = re.search(r"\bI am (?:the |a |an )?([A-Z][A-Za-z &/\-]{2,60}?)\s+(?:at|of|for|from)\b", body)
    return _clip(m.group(1)) if m else None


def _phrases(text: str, phrases: list[str]) -> bool:
    low = text.lower()
    return any(re.search(r"(?<!\w)" + re.escape(p.lower()) + r"(?!\w)", low) for p in phrases)


# ---------- main ----------

def extract(raw_text: str) -> Extraction:
    text = normalise_text(raw_text)
    inv = Invoice(None, None, None, None, None, None, None, None, None, raw_text=raw_text or "")
    ex = Extraction(inv)
    if not text.strip():
        ex.flags["empty"] = True
        return ex

    block, forwarded = _message_block(text)
    ex.flags["forwarded"] = forwarded
    hdr = _headers(block)
    body = _strip_quoted(HEADER.sub("", block))
    subject = hdr.get("subject", "")

    sender_name, sender_email = _parse_address(hdr["from"]) if "from" in hdr else (None, None)
    if not sender_email:   # no header: first address in the message
        m = EMAIL.search(block)
        sender_email = normalise_email(m.group(0)) if m else None
    inv.sender_email, inv.sender_name = sender_email, _clip(sender_name)
    ex.flags["display_name"] = sender_name
    if "reply-to" in hdr:
        inv.reply_to_email = _parse_address(hdr["reply-to"])[1]
    if "return-path" in hdr:
        inv.return_path_email = _parse_address(hdr["return-path"])[1]
    inv.invoice_date = _clip(hdr.get("date"))

    inv.vendor_name, inv.vendor_domain = _vendor(body)
    inv.sender_title = _title(body, inv.sender_name)
    m = ADDRESS.search(body)
    inv.vendor_address = _clip(m.group(1)) if m else None
    m = INVOICE_NO.search(subject) or INVOICE_NO.search(body)
    inv.invoice_number = _clip(m.group(1)) if m else None

    # amounts (E6-E9)
    amounts = find_amounts(body, subject)
    labelled = [a for a in amounts if a.labelled]
    chosen = labelled[0] if labelled else (amounts[0] if amounts else None)
    if chosen:
        inv.amount, inv.currency = chosen.value, chosen.currency
        if chosen.assumed:
            ex.notes.append("currency '$' assumed USD")
    distinct = {(a.currency, round(a.value, 2)) for a in labelled}
    if len(distinct) > 1:
        vals = ", ".join(f"{c} {v:,.2f}" for c, v in sorted(distinct))
        ex.evidence.append(Evidence(
            "X2_amount_conflict", "INTEGRITY", "Invoice states one total", "contradicted",
            f"labelled totals disagree ({vals}); using {chosen.currency} {chosen.value:,.2f}",
            "extract:regex#amount", weight=10))
    ex.flags["credit_note"] = bool(CREDIT_NOTE.search(body) or CREDIT_NOTE.search(subject))
    ex.flags["amounts_found"] = len(amounts)

    # bank (E10-E12)
    bank = find_bank(body)
    inv.bank_iban = bank["iban"][0] if bank["iban"] else None
    inv.bank_account = bank["account"][0] if bank["account"] else None
    inv.bank_swift = (bank["swift"] or bank["ifsc"] or [None])[0]
    acct = inv.bank_iban or inv.bank_account
    inv.bank_account_last4 = acct[-4:] if acct else None
    accounts = bank["iban"] + bank["account"]
    if len(accounts) > 1:
        ex.evidence.append(Evidence(
            "G5_ambiguous_payment", "PAYMENT", "Email gives one payment account", "contradicted",
            f"two different accounts given ({', '.join('••' + a[-4:] for a in accounts)})",
            "extract:regex#bank_account", weight=25))
    ex.flags["accounts"] = len(accounts)
    ex.flags["account_on_file"] = bool(ACCOUNT_ON_FILE.search(body)) and not accounts
    inv.remit_country = remit_country(_country_from_text(body), inv.bank_iban, inv.bank_swift if bank["swift"] else None,
                                      bank["ifsc"][0] if bank["ifsc"] else None)

    tactics = policy()["tactics"]
    inv.urgency_language = _phrases(subject + "\n" + body, tactics.get("urgency", []))
    inv.bank_change_requested = bool(BANK_CHANGE.search(subject + "\n" + body))
    ex.flags["not_payment"] = inv.amount is None and inv.invoice_number is None and not accounts \
        and not (bank["swift"] or bank["ifsc"]) and not inv.bank_change_requested
    ex.flags["body"] = body          # quoted history removed; used by tactic checks
    return ex


def cross_check(ex: Extraction, llm_fields: Optional[dict[str, Any]]) -> Extraction:
    """X1: compare Claude-supplied fields with the regex. Regex wins for critical fields."""
    if not llm_fields:
        return ex
    inv = ex.invoice
    raw_low = normalise_text(inv.raw_text).lower()
    known = {f.name for f in fields(Invoice)} - {"raw_text"}
    updates: dict[str, Any] = {}
    for name, val in llm_fields.items():
        if name not in known or val is None or val == "":
            continue
        if isinstance(val, str):
            val = val.strip()[:MAX_FIELD]
        mine = getattr(inv, name)
        if name in CRITICAL:
            theirs, ours = _norm_field(name, val), _norm_field(name, mine)
            if theirs is None:
                continue
            if ours is not None and theirs != ours:
                ex.evidence.append(Evidence(
                    "X1_extraction", "INTEGRITY", f"Supplied {name} matches the email text", "contradicted",
                    f"{name}: text says {_show(name, ours)}, caller supplied {_show(name, theirs)}; text value used",
                    f"extract:regex#{name} vs extract:llm#{name}", weight=10))
            elif ours is None:
                if _appears(name, theirs, raw_low):
                    updates[name] = theirs
                else:
                    ex.evidence.append(Evidence(
                        "X1_extraction", "INTEGRITY", f"Supplied {name} matches the email text", "contradicted",
                        f"{name}: caller supplied {_show(name, theirs)}, which is not in the email text; ignored",
                        f"extract:llm#{name}", weight=10))
        elif name in GROUNDED:
            theirs = str(val).lower()
            if mine is None and theirs in raw_low:
                updates[name] = theirs
            elif mine is None or theirs != mine.lower():
                ex.evidence.append(Evidence(
                    "X1_extraction", "INTEGRITY", f"Supplied {name} matches the email text", "contradicted",
                    f"{name}: caller supplied {theirs}, text says {mine or 'nothing'}; text value used",
                    f"extract:regex#{name} vs extract:llm#{name}", weight=10))
        elif isinstance(mine, bool):
            updates[name] = mine or bool(val)          # caution only ever increases
        elif mine is None:
            updates[name] = val
    ex.invoice = replace(inv, **updates)
    if ex.invoice.bank_iban or ex.invoice.bank_account:
        acct = ex.invoice.bank_iban or ex.invoice.bank_account
        ex.invoice.bank_account_last4 = acct[-4:]
    return ex


def _norm_field(name: str, v: Any) -> Any:
    if v is None or v == "":
        return None
    if name in ("sender_email", "reply_to_email"):
        return normalise_email(str(v))
    if name == "bank_iban":
        return normalise_iban(str(v))
    if name == "bank_account":
        return normalise_account(str(v))
    if name == "bank_swift":
        return str(v).strip().upper()
    if name == "amount":
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None
    return v


def _appears(name: str, v: Any, raw_low: str) -> bool:
    if name == "amount":
        return any(abs(a.value - v) < 0.01 for a in find_amounts(raw_low.upper()))
    if name in ("bank_iban", "bank_account"):
        return str(v).lower() in re.sub(r"[\s\-]", "", raw_low)
    return str(v).lower() in raw_low


def _show(name: str, v: Any) -> str:
    """Never echo full account numbers (Y2)."""
    if name in ("bank_iban", "bank_account"):
        return "••" + str(v)[-4:]
    return str(v)
