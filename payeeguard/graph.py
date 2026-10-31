"""Payment-trust graph (spec section 8): SQLite nodes/edges with a citation on every edge.

Raw account numbers never touch the database: accounts are stored only as HMAC fingerprints,
labelled with country + last 4. Every query excludes the current case (G-1) and ignores
revoked edges (G-7).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from payeeguard.models import Invoice

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "samples" / "seed_history.json"
SEED_CITATION = "seed:samples/seed_history.json"
SEED_CASE = "seed"
MIN_SECRET_LEN = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, type TEXT, label TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS edges(src TEXT, rel TEXT, dst TEXT, citation TEXT, observed_at TEXT,
                                 case_id TEXT, revoked_at TEXT, UNIQUE(src, rel, dst, case_id));
CREATE INDEX IF NOT EXISTS e_dst ON edges(dst, rel);
CREATE INDEX IF NOT EXISTS e_src ON edges(src, rel);
CREATE TABLE IF NOT EXISTS payments(case_id TEXT, vendor TEXT, invoice_no TEXT, account_ids TEXT,
                                    account_label TEXT, amount REAL, currency TEXT, paid_on TEXT,
                                    citation TEXT, revoked_at TEXT,
                                    UNIQUE(case_id, vendor, invoice_no, paid_on, amount));
CREATE INDEX IF NOT EXISTS p_vendor ON payments(vendor, invoice_no);
"""

_SWIFT_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


class GraphConfigError(RuntimeError):
    """The graph refuses to start (e.g. PAYEEGUARD_FP_SECRET missing, G-11)."""


class SeedError(RuntimeError):
    """`seed` could not complete; the message says what to do (G-9)."""


# ---------------------------------------------------------------- normalisation helpers

def _alnum(value: Optional[str]) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value or "").upper()


def normalise_iban(iban: Optional[str]) -> Optional[str]:
    """E12: uppercase, no spaces/dashes; tolerates a leading 'IBAN' label."""
    s = _alnum(iban)
    if s.startswith("IBAN") and len(s) > 4 and s[4:6].isalpha():
        s = s[4:]
    return s or None


def is_ifsc(code: Optional[str]) -> bool:
    return bool(code) and bool(_IFSC_RE.match(_alnum(code)))


def normalise_bank_code(code: Optional[str]) -> Optional[str]:
    """SWIFT or IFSC, uppercase. An 11-char SWIFT ending in XXX is the head office = the 8-char code."""
    s = _alnum(code)
    if not s:
        return None
    if not is_ifsc(s) and len(s) == 11 and s.endswith("XXX") and _SWIFT_RE.match(s):
        return s[:8]
    return s


def normalise_account(account: Optional[str]) -> Optional[str]:
    return _alnum(account) or None


def normalise_domain(domain: Optional[str]) -> Optional[str]:
    """Lowercase host: drops scheme, path, port, 'www.' and a trailing dot."""
    s = (domain or "").strip().lower()
    if not s:
        return None
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0].rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    return s or None


def normalise_email(email: Optional[str]) -> Optional[str]:
    s = (email or "").strip().lower().rstrip(".")
    if s.startswith("mailto:"):
        s = s[7:]
    return s if "@" in s else None


def email_domain(email: Optional[str]) -> Optional[str]:
    e = normalise_email(email)
    return normalise_domain(e.rsplit("@", 1)[1]) if e else None


def remit_country(stated: Optional[str], iban: Optional[str], code: Optional[str]) -> Optional[str]:
    """Spec section 7: stated -> IBAN prefix -> SWIFT chars 5-6 -> IFSC (-> IN)."""
    if stated and len(stated.strip()) == 2 and stated.strip().isalpha():
        return stated.strip().upper()
    i = normalise_iban(iban)
    if i and len(i) >= 2 and i[:2].isalpha():
        return i[:2]
    c = normalise_bank_code(code)
    if c and is_ifsc(c):
        return "IN"
    if c and _SWIFT_RE.match(c):
        return c[4:6]
    return None


def mask_last4(value: Optional[str]) -> Optional[str]:
    s = _alnum(value)
    return s[-4:] if len(s) >= 4 else None


def case_id_for(raw_text: str) -> str:
    """sha256(normalised raw_text)[:12]; normalised = whitespace collapsed, stripped."""
    norm = " ".join((raw_text or "").split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- node ids

def vendor_node(company_id: Any = None, vendor_domain: Optional[str] = None) -> Optional[str]:
    """`company:<crustdata id>`; a vendor Crustdata doesn't know falls back to `vendor:<domain>` (S2)."""
    if company_id not in (None, ""):
        try:
            return f"company:{int(company_id)}"
        except (TypeError, ValueError):
            pass
    d = normalise_domain(vendor_domain)
    return f"vendor:{d}" if d else None


def domain_node(domain: Optional[str]) -> Optional[str]:
    d = normalise_domain(domain)
    return f"domain:{d}" if d else None


def email_node(email: Optional[str]) -> Optional[str]:
    e = normalise_email(email)
    return f"email:{hashlib.sha1(e.encode('utf-8')).hexdigest()}" if e else None


def invoice_node(vendor: Optional[str], invoice_no: Optional[str], case_id: str) -> str:
    no = normalise_invoice_no(invoice_no) or f"case-{case_id}"
    return f"invoice:{vendor or 'unknown'}:{no}"


def normalise_invoice_no(invoice_no: Optional[str]) -> Optional[str]:
    s = re.sub(r"\s+", "", (invoice_no or "")).upper()
    return s or None


def case_node(case_id: str) -> str:
    return f"case:{case_id}"


def short(node_id: str, n: int = 12) -> str:
    """`account:9be1c0ffee…` for citations and pictures."""
    prefix, _, rest = node_id.rpartition(":")
    return f"{prefix}:{rest[:n]}…" if len(rest) > n else node_id


def graph_citation(rel: str, src: str, dst: str, case_id: str) -> str:
    return f"graph:{rel}/{short(src)}->{short(dst)}@case:{case_id}"


# ---------------------------------------------------------------- fingerprints

@dataclass(frozen=True)
class Fingerprint:
    """Account identity without the account number.

    `strong` (IBAN, or bank code + full account) may drive hard decisions. `alias` (full account
    number without a bank code) links the same number given with or without a code: good for
    history and G2, never hard, because two banks can issue the same number. `weak` (bank code +
    last 4) is only for G1 history comparison (G-5).
    """
    strong: tuple[str, ...] = ()
    weak: Optional[str] = None
    label: str = ""                  # "••5432 GB": safe to show
    alias: Optional[str] = None

    @property
    def matchable(self) -> tuple[str, ...]:
        """Full-number ids: strong + alias."""
        return self.strong + ((self.alias,) if self.alias else ())

    @property
    def ids(self) -> tuple[str, ...]:
        return self.matchable + ((self.weak,) if self.weak else ())

    @property
    def primary(self) -> Optional[str]:
        return (self.matchable or (self.weak,))[0]

    @property
    def empty(self) -> bool:
        return not self.ids

    def alias_conflict(self, other_ids: Iterable[str]) -> bool:
        """True when an alias-only match is false: both sides name a bank code and the strong ids
        differ, so it's the same number at two different banks."""
        others = set(other_ids)
        other_strong = {i for i in others if is_strong_id(i)}
        return bool(self.strong) and bool(other_strong) and not (other_strong & set(self.strong))


def is_strong_id(node_id: str) -> bool:
    return node_id.startswith("account:") and not node_id.startswith(("account:alias:", "account:weak:"))


# ---------------------------------------------------------------- the graph

class Graph:
    def __init__(self, path: str | Path | None = None, secret: Optional[str] = None) -> None:
        secret = secret if secret is not None else os.environ.get("PAYEEGUARD_FP_SECRET", "")
        if not secret or not secret.strip():
            raise GraphConfigError("PAYEEGUARD_FP_SECRET missing: set it in .env "
                                   "(generate with: openssl rand -hex 32)")
        if len(secret.strip()) < MIN_SECRET_LEN:
            raise GraphConfigError(f"PAYEEGUARD_FP_SECRET too short: use at least {MIN_SECRET_LEN} "
                                   "characters (openssl rand -hex 32)")
        self._secret = secret.strip().encode("utf-8")
        self.path = str(path) if path is not None else str(default_db_path())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def reset(self) -> None:
        """Empty every table (used by `eval` to start from a fresh graph)."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM edges")
            self._db.execute("DELETE FROM nodes")
            self._db.execute("DELETE FROM payments")

    # ------------------------------------------------------------ fingerprints

    def _hmac(self, normalised: str) -> str:
        return "account:" + hmac.new(self._secret, normalised.encode("utf-8"), hashlib.sha256).hexdigest()

    def fingerprint(self, iban: Optional[str] = None, account: Optional[str] = None,
                    code: Optional[str] = None, last4: Optional[str] = None,
                    country: Optional[str] = None) -> Fingerprint:
        i, a, c = normalise_iban(iban), normalise_account(account), normalise_bank_code(code)
        strong: list[str] = []
        if i:
            strong.append(self._hmac(i))
        if a and c:
            strong.append(self._hmac(f"{c}:{a}"))
        alias = "account:alias:" + self._hmac(f":{a}")[8:] if a and len(a) >= 6 else None
        tail = mask_last4(i) or mask_last4(a) or mask_last4(last4)
        weak = None
        if c and tail:
            weak = "account:weak:" + hashlib.sha256(f"{c}{tail}".encode("utf-8")).hexdigest()
        cc = remit_country(country, i, c)
        label = " ".join(x for x in (f"••{tail}" if tail else "", cc or "") if x)
        return Fingerprint(tuple(dict.fromkeys(strong)), weak, label or "account", alias)

    def fingerprint_invoice(self, inv: Invoice) -> Fingerprint:
        return self.fingerprint(inv.bank_iban, inv.bank_account, inv.bank_swift,
                                inv.bank_account_last4, inv.remit_country)

    # ------------------------------------------------------------ low-level writes

    def add_node(self, node_id: str, type_: str, label: str = "") -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO nodes(id, type, label, first_seen) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET label = CASE WHEN excluded.label != '' "
                "THEN excluded.label ELSE nodes.label END",
                (node_id, type_, label, _now()))

    def add_edge(self, src: str, rel: str, dst: str, citation: str, case_id: str,
                 observed_at: Optional[str] = None) -> bool:
        """Idempotent: the same (src, rel, dst, case) is written once. Returns True if new."""
        if not citation:
            raise ValueError("every graph edge needs a citation")
        if not case_id:
            raise ValueError("every graph edge needs a case_id")
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO edges(src, rel, dst, citation, observed_at, case_id) VALUES(?,?,?,?,?,?)",
                (src, rel, dst, citation, observed_at or _now(), case_id))
            return cur.rowcount > 0

    def _account_nodes(self, fp: Fingerprint, ids: Iterable[str]) -> None:
        for node_id in ids:
            self.add_node(node_id, "account", f"Account {fp.label}")

    # ------------------------------------------------------------ verify-time writes (idempotent, G-2)

    def record_identity(self, case_id: str, company_id: Any, name: str = "",
                        domains: Iterable[str] = ()) -> None:
        """OWNS_DOMAIN company -> domain, from Crustdata basic_info.all_domains (C1)."""
        company = vendor_node(company_id)
        if not company:
            return
        self.add_node(company, "company", name)
        for d in domains:
            dn = domain_node(d)
            if dn:
                self.add_node(dn, "domain", normalise_domain(d) or "")
                self.add_edge(company, "OWNS_DOMAIN", dn,
                              f"crustdata:company/{int(company_id)}#basic_info.all_domains", case_id)

    def record_employment(self, case_id: str, person_id: Any, company_id: Any, current: bool,
                          label: str = "", field_path: str = "") -> None:
        """EMPLOYED_AT_CURRENT / EMPLOYED_AT_PAST person -> company (C3)."""
        company = vendor_node(company_id)
        if person_id in (None, "") or not company:
            return
        person = f"person:{person_id}"
        self.add_node(person, "person", label)
        self.add_node(company, "company")
        rel = "EMPLOYED_AT_CURRENT" if current else "EMPLOYED_AT_PAST"
        path = field_path or f"experience.employment_details.{'current' if current else 'past'}"
        self.add_edge(person, rel, company, f"crustdata:person/{person_id}#{path}", case_id)

    def record_leads(self, case_id: str, person_id: Any, company_id: Any, label: str = "",
                     group: str = "people") -> None:
        """LEADS person -> company (insider check, P1)."""
        company = vendor_node(company_id)
        if person_id in (None, "") or not company:
            return
        person = f"person:{person_id}"
        self.add_node(person, "person", label)
        self.add_edge(person, "LEADS", company, f"crustdata:company/{int(company_id)}#{group}", case_id)

    def record_invoice(self, case_id: str, inv: Invoice, vendor: Optional[str],
                       fp: Optional[Fingerprint] = None) -> str:
        """SENT email -> invoice, CLAIMS_VENDOR invoice -> vendor, PAYS_TO invoice -> account."""
        fp = fp or self.fingerprint_invoice(inv)
        inv_id = invoice_node(vendor, inv.invoice_number, case_id)
        self.add_node(inv_id, "invoice", normalise_invoice_no(inv.invoice_number) or "invoice")
        sender = email_node(inv.sender_email)
        if sender:
            self.add_node(sender, "email", email_domain(inv.sender_email) or "")
            self.add_edge(sender, "SENT", inv_id, "extract:regex#sender_email", case_id)
        if vendor:
            self.add_node(vendor, "company" if vendor.startswith("company:") else "vendor", inv.vendor_name or "")
            source = (f"crustdata:company/{vendor.split(':', 1)[1]}#basic_info"
                      if vendor.startswith("company:") else "extract:regex#vendor_domain")
            self.add_edge(inv_id, "CLAIMS_VENDOR", vendor, source, case_id)
        if fp.primary:
            self._account_nodes(fp, [fp.primary])
            field = "bank_iban" if inv.bank_iban else "bank_account"
            self.add_edge(inv_id, "PAYS_TO", fp.primary, f"extract:regex#{field}", case_id)
        return inv_id

    # ------------------------------------------------------------ gate-time writes

    def record_payment(self, case_id: str, vendor: str, fp: Fingerprint, invoice_no: Optional[str],
                       amount: Optional[float], currency: Optional[str],
                       sender_domain: Optional[str] = None, paid_on: Optional[str] = None,
                       citation: Optional[str] = None) -> bool:
        """PAID company -> account (+ PAID_FROM_DOMAIN). Only the gate or a human approval calls this.

        Idempotent per case: paying the same case twice records one payment (G-3).
        Returns True if a new payment row was written.
        """
        if not vendor:
            raise ValueError("record_payment needs a vendor node")
        if fp.empty:
            raise ValueError("record_payment needs an account fingerprint (use the ledger account for E11)")
        paid_on = paid_on or _now()[:10]
        cite = citation or f"gate:pay_invoice@case:{case_id}"
        self.add_node(vendor, "company" if vendor.startswith("company:") else "vendor")
        self._account_nodes(fp, fp.ids)
        for acc in fp.ids:
            self.add_edge(vendor, "PAID", acc, cite, case_id, observed_at=paid_on)
        dn = domain_node(sender_domain)
        if dn:
            self.add_node(dn, "domain", normalise_domain(sender_domain) or "")
            self.add_edge(vendor, "PAID_FROM_DOMAIN", dn, cite, case_id, observed_at=paid_on)
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO payments(case_id, vendor, invoice_no, account_ids, account_label, "
                "amount, currency, paid_on, citation) VALUES(?,?,?,?,?,?,?,?,?)",
                (case_id, vendor, normalise_invoice_no(invoice_no) or "", " ".join(fp.ids), fp.label,
                 float(amount) if amount is not None else None, (currency or "").upper() or None,
                 paid_on, cite))
            return cur.rowcount > 0

    def record_block(self, case_id: str, fp: Optional[Fingerprint] = None,
                     sender_email: Optional[str] = None, sender_domain: Optional[str] = None,
                     label: str = "", citation: Optional[str] = None) -> list[str]:
        """FLAGGED_IN account / domain / email -> case. Only on BLOCK. Weak ids are never flagged (G-5).

        Pass `sender_domain` only when the domain itself is bad (lookalike): a compromised real
        mailbox (sample 06) must not flag the vendor's real domain.
        """
        case = case_node(case_id)
        self.add_node(case, "case", label or f"Case {case_id[:4]} BLOCKED")
        cite = citation or f"decision:BLOCK@case:{case_id}"
        flagged: list[str] = []
        if fp and fp.matchable:
            self._account_nodes(fp, fp.matchable)
            flagged += list(fp.matchable)
        en = email_node(sender_email)
        if en:
            self.add_node(en, "email", email_domain(sender_email) or "")
            flagged.append(en)
        dn = domain_node(sender_domain)
        if dn:
            self.add_node(dn, "domain", dn.split(":", 1)[1])
            flagged.append(dn)
        for node in flagged:
            self.add_edge(node, "FLAGGED_IN", case, cite, case_id)
        return flagged

    def revoke_case(self, case_id: str, when: Optional[str] = None) -> int:
        """G-7: a human overrode a wrong BLOCK; its FLAGGED_IN edges stop counting. Returns edges revoked."""
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE edges SET revoked_at = ? WHERE case_id = ? AND rel = 'FLAGGED_IN' AND revoked_at IS NULL",
                (when or _now(), case_id))
            return cur.rowcount

    # ------------------------------------------------------------ queries (G-1: exclude_case; G-7: not revoked)

    def _rows(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def accounts_paid(self, company: Optional[str], exclude_case: Optional[str] = None) -> list[str]:
        if not company:
            return []
        rows = self._rows("SELECT DISTINCT dst FROM edges WHERE src = ? AND rel = 'PAID' "
                          "AND case_id != ? AND revoked_at IS NULL ORDER BY dst", (company, exclude_case or ""))
        return [r["dst"] for r in rows]

    def companies_paid(self, account_ids: Iterable[str], exclude_case: Optional[str] = None) -> list[str]:
        ids = [a for a in account_ids if a]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._rows(f"SELECT DISTINCT src FROM edges WHERE dst IN ({marks}) AND rel = 'PAID' "
                          "AND case_id != ? AND revoked_at IS NULL ORDER BY src", (*ids, exclude_case or ""))
        return [r["src"] for r in rows]

    def cases_flagged(self, node_ids: Iterable[str], exclude_case: Optional[str] = None) -> list[dict]:
        ids = [n for n in node_ids if n]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._rows(
            f"SELECT e.src, e.dst, e.case_id, e.observed_at, e.citation, n.label FROM edges e "
            f"LEFT JOIN nodes n ON n.id = e.dst WHERE e.src IN ({marks}) AND e.rel = 'FLAGGED_IN' "
            "AND e.case_id != ? AND e.revoked_at IS NULL ORDER BY e.observed_at, e.case_id",
            (*ids, exclude_case or ""))
        return [dict(r) for r in rows]

    def payments(self, vendor: Optional[str], exclude_case: Optional[str] = None) -> list[dict]:
        if not vendor:
            return []
        rows = self._rows("SELECT * FROM payments WHERE vendor = ? AND case_id != ? AND revoked_at IS NULL "
                          "ORDER BY paid_on, invoice_no", (vendor, exclude_case or ""))
        return [dict(r) | {"account_ids": r["account_ids"].split()} for r in rows]

    def invoice_paid(self, vendor: Optional[str], invoice_no: Optional[str],
                     exclude_case: Optional[str] = None) -> list[dict]:
        no = normalise_invoice_no(invoice_no)
        if not vendor or not no:
            return []
        return [p for p in self.payments(vendor, exclude_case) if p["invoice_no"] == no]

    def domains_paid_from(self, vendor: Optional[str], exclude_case: Optional[str] = None) -> list[str]:
        if not vendor:
            return []
        rows = self._rows("SELECT DISTINCT dst FROM edges WHERE src = ? AND rel = 'PAID_FROM_DOMAIN' "
                          "AND case_id != ? AND revoked_at IS NULL ORDER BY dst", (vendor, exclude_case or ""))
        return [r["dst"].split(":", 1)[1] for r in rows]

    def vendor_known(self, vendor: Optional[str], exclude_case: Optional[str] = None) -> bool:
        """10.3 'who': the vendor has PAID edges (other than from this case)."""
        return bool(self.accounts_paid(vendor, exclude_case))

    def is_case_paid(self, case_id: str) -> bool:
        return bool(self._rows("SELECT 1 FROM payments WHERE case_id = ? AND revoked_at IS NULL LIMIT 1",
                               (case_id,)))

    def node_label(self, node_id: str) -> str:
        rows = self._rows("SELECT label FROM nodes WHERE id = ?", (node_id,))
        return (rows[0]["label"] if rows else "") or ""

    def edges_for_case(self, case_id: str) -> list[dict]:
        """Edges written by this case, oldest first."""
        rows = self._rows("SELECT src, rel, dst, citation, observed_at, case_id, revoked_at FROM edges "
                          "WHERE case_id = ? ORDER BY observed_at, rel, src, dst", (case_id,))
        return [dict(r) for r in rows]

    def money_trail(self, case_id: str) -> list[dict]:
        """The Mermaid money-trail picture for a case: its invoice, the vendor's paid accounts
        (aggregated with a count) and blocked cases that share the account. Every row is cited."""
        out: list[dict] = []
        own = self.edges_for_case(case_id)
        vendors = [e["dst"] for e in own if e["rel"] == "CLAIMS_VENDOR"]
        accounts = [e["dst"] for e in own if e["rel"] == "PAYS_TO"]
        for e in own:
            if e["rel"] in ("CLAIMS_VENDOR", "PAYS_TO"):
                out.append(self._trail_row(e["src"], e["rel"], e["dst"], e["citation"]))
        for v in vendors:
            counts: dict[str, int] = {}
            for p in self.payments(v, case_id):
                acc = p["account_ids"][0] if p["account_ids"] else None
                if acc:
                    counts[acc] = counts.get(acc, 0) + 1
            for acc, n in counts.items():
                out.append(self._trail_row(v, "PAID", acc, graph_citation("PAID", v, acc, "history"), n))
        for acc in accounts:
            for f in self.cases_flagged([acc], case_id):
                out.append(self._trail_row(acc, "FLAGGED_IN", f["dst"], f["citation"]))
        return out

    def _trail_row(self, src: str, rel: str, dst: str, citation: str, count: int = 1) -> dict:
        return {"src": src, "rel": rel, "dst": dst, "citation": citation, "count": count,
                "src_label": self.node_label(src) or short(src), "dst_label": self.node_label(dst) or short(dst)}


# ---------------------------------------------------------------- process-wide graph (G-10)

_GRAPHS: dict[str, Graph] = {}
_GRAPHS_LOCK = threading.Lock()


def default_db_path(name: str = "graph.db") -> Path:
    data = Path(os.environ.get("PAYEEGUARD_DATA_DIR") or "data")
    return (data if data.is_absolute() else REPO_ROOT / data) / name


def get_graph(path: str | Path | None = None) -> Graph:
    """One connection per process and database file."""
    key = str(Path(path).resolve()) if path not in (None, ":memory:") else str(path or default_db_path())
    with _GRAPHS_LOCK:
        if key not in _GRAPHS:
            _GRAPHS[key] = Graph(path)
        return _GRAPHS[key]


# ---------------------------------------------------------------- seed loader (`cli.py seed`)

def _identify_company_id(client: Any, domain: str) -> Optional[int]:
    """Company Identify (free) by exact domain; best match with confidence >= 0.8, else None."""
    resp = client.post("/company/identify", {"domains": [domain], "exact_match": True})
    groups = resp if isinstance(resp, list) else (resp or {}).get("results", []) if isinstance(resp, dict) else []
    best: tuple[float, Optional[int]] = (0.0, None)
    for group in groups:
        for m in (group or {}).get("matches") or []:
            data = m.get("company_data") or {}
            cid = data.get("crustdata_company_id") or (data.get("basic_info") or {}).get("crustdata_company_id")
            conf = float(m.get("confidence_score") or 0)
            if cid is not None and conf > best[0]:
                best = (conf, int(cid))
    return best[1] if best[0] >= 0.8 else None


def load_seed(graph: Graph, client: Any, path: str | Path = SEED_PATH) -> dict:
    """Load samples/seed_history.json: resolve each vendor with Company Identify, fingerprint its
    account with HMAC, write PAID / PAID_FROM_DOMAIN edges + payment rows. Safe to run twice."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SeedError(f"cannot read seed file {path}: {exc}") from exc
    summary: dict[str, Any] = {"vendors": [], "payments": 0}
    for v in data.get("vendors", []):
        domain = normalise_domain(v.get("domain"))
        if not domain:
            raise SeedError(f"seed vendor {v.get('name')!r} has no domain")
        cid = v.get("crustdata_company_id")
        if cid is None:
            if client is None:
                raise SeedError("seed needs a Crustdata client to resolve vendor domains")
            try:
                cid = _identify_company_id(client, domain)
            except Exception as exc:  # OfflineMiss, NotEnabled, network: say what to do
                raise SeedError(
                    f"seed could not resolve {domain} with Company Identify ({type(exc).__name__}: {exc}). "
                    "Run `seed` once online with CRUSTDATA_API_KEY set, or pull the committed .cache/ "
                    "(offline mode only reads the cache).") from exc
        if cid is None:
            log.warning("seed: %s not found in Crustdata; storing it as vendor:%s", domain, domain)
        vendor = vendor_node(cid, domain)
        assert vendor
        graph.add_node(vendor, "company" if cid is not None else "vendor", v.get("name") or domain)
        acc = v.get("account") or {}
        fp = graph.fingerprint(acc.get("iban"), acc.get("account"), acc.get("swift") or acc.get("ifsc"),
                               acc.get("last4"), acc.get("country"))
        if fp.empty:
            raise SeedError(f"seed vendor {domain} has no usable account")
        written = 0
        for p in v.get("payments", []):
            written += graph.record_payment(SEED_CASE, vendor, fp, p.get("invoice_number"), p.get("amount"),
                                            p.get("currency") or v.get("currency"),
                                            v.get("sender_domain") or domain, p.get("paid_on"), SEED_CITATION)
        summary["vendors"].append({"vendor": vendor, "name": v.get("name"), "account": fp.label,
                                   "payments": len(v.get("payments", [])), "new": written})
        summary["payments"] += len(v.get("payments", []))
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    """`python -m payeeguard.graph seed [--db PATH] [--fresh]` until cli.py wires `seed` in."""
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(prog="payeeguard.graph")
    ap.add_argument("command", choices=["seed"])
    ap.add_argument("--db", default=None, help="database path (default data/graph.db)")
    ap.add_argument("--fresh", action="store_true", help="empty the graph first")
    args = ap.parse_args(argv)
    try:
        graph = get_graph(args.db)
        if args.fresh:
            graph.reset()
        from payeeguard import client as client_mod  # Yaksh's module
        client = getattr(client_mod, "get_client", None)
        summary = load_seed(graph, client() if callable(client) else client_mod)
    except (GraphConfigError, SeedError, ImportError) as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    for v in summary["vendors"]:
        print(f"seeded {v['name']} ({v['vendor']}): {v['payments']} payments to {v['account']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
