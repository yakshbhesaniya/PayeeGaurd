"""Payment-trust graph (spec section 8): SQLite nodes/edges with a citation on every edge.

Crustdata gives identity edges (company -> domain, person -> employer); we add money edges
(invoice -> account, company -PAID-> account) and fraud edges (account -FLAGGED_IN-> blocked case).
Raw account numbers are never stored: only keyed HMAC fingerprints.
"""

import hashlib
import hmac
import json
import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, geo

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, type TEXT, label TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS edges(src TEXT, rel TEXT, dst TEXT, citation TEXT, observed_at TEXT,
                                 case_id TEXT NOT NULL DEFAULT '', revoked_at TEXT,
                                 UNIQUE(src, rel, dst, case_id));
CREATE INDEX IF NOT EXISTS e_dst ON edges(dst, rel);
CREATE INDEX IF NOT EXISTS e_src ON edges(src, rel);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, weak TEXT, country TEXT, last4 TEXT);
CREATE TABLE IF NOT EXISTS payments(case_id TEXT, company TEXT, invoice_no TEXT, amount REAL, currency TEXT,
                                    account TEXT, paid_on TEXT, source TEXT, UNIQUE(case_id, company, invoice_no));
CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, decision TEXT, state TEXT, data TEXT,
                                 created_at TEXT, updated_at TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Fingerprint:
    id: str               # node id: "account:<hmac>" (strong) or "account:weak:<hmac>"
    weak: str | None      # weak id (code + last4), for comparing against history
    strong: bool
    country: str | None
    last4: str | None

    @property
    def masked(self) -> str:
        return f"••••{self.last4 or '????'} {self.country or ''}".strip()


class Graph:
    def __init__(self, path: Path | None = None):
        secret = config.env("PAYEEGUARD_FP_SECRET").strip()
        if not secret:  # edge case G-11: refuse an insecure default (whitespace counts as missing)
            raise RuntimeError("PAYEEGUARD_FP_SECRET missing in .env (generate with: openssl rand -hex 32)")
        self._secret = secret.encode()
        self.path = path or config.data_dir() / "graph.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- fingerprints -----------------------------------------------------------------------------
    def _h(self, s: str) -> str:
        return hmac.new(self._secret, s.encode(), hashlib.sha256).hexdigest()[:24]

    def fingerprint(self, iban=None, account=None, code=None, last4=None) -> Fingerprint | None:
        strong_key = None
        country = None
        code = normalise_code(code)
        if iban:
            n = geo.iban_normalise(iban)
            strong_key, country, last4 = "IBAN:" + n, n[:2], n[-4:]
        elif account and code:
            digits = re.sub(r"\D", "", account)
            strong_key, last4 = f"{code}:{digits}", digits[-4:]
        elif account:
            # Full account number but no bank code: can't be a strong identity (could exist at another bank), but it is
            # definitely a specific account, so it never matches history implicitly -> G1 sees a NEW account.
            digits = re.sub(r"\D", "", account)
            if len(digits) >= 6:
                return Fingerprint("account:nocode:" + self._h(digits), None, False, None, digits[-4:])
        if code:
            country = country or ("IN" if geo.is_ifsc(code) else geo.swift_country(code))
        weak_code = (code or (iban[:2] if iban else "") or "").upper()
        weak = "account:weak:" + self._h(f"{weak_code}:{last4}") if last4 and weak_code else None
        if strong_key:
            return Fingerprint("account:" + self._h(strong_key), weak, True, country, last4)
        if weak:
            return Fingerprint(weak, weak, False, country, last4)
        return None

    # ---- writes -----------------------------------------------------------------------------------
    def node(self, nid: str, ntype: str, label: str = "") -> str:
        self.db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?,?)", (nid, ntype, label, now()))
        return nid

    def edge(self, src: str, rel: str, dst: str, citation: str, case_id: str = "") -> None:
        self.db.execute("INSERT OR IGNORE INTO edges(src,rel,dst,citation,observed_at,case_id) VALUES (?,?,?,?,?,?)",
                        (src, rel, dst, citation, now(), case_id))

    def account(self, fp: Fingerprint) -> str:
        self.node(fp.id, "account", fp.masked)
        self.db.execute("INSERT OR IGNORE INTO accounts VALUES (?,?,?,?)", (fp.id, fp.weak, fp.country, fp.last4))
        return fp.id

    def record_payment(self, case_id, company, invoice_no, amount, currency, fp: Fingerprint | None,
                       paid_on, source, domain=None) -> None:
        acct = self.account(fp) if fp else None
        self.db.execute("INSERT OR IGNORE INTO payments VALUES (?,?,?,?,?,?,?,?)",
                        (case_id, company, invoice_no, amount, currency, acct, paid_on, source))
        if acct:
            self.edge(company, "PAID", acct, source, case_id)
        if domain:
            self.edge(company, "PAID_FROM_DOMAIN", self.node(f"domain:{domain}", "domain", domain), source, case_id)
        self.db.commit()

    def flag(self, case_id: str, nodes: list[str], citation: str) -> None:
        case = self.node(f"case:{case_id}", "case", case_id)
        for n in nodes:
            self.edge(n, "FLAGGED_IN", case, citation, case_id)
        self.db.commit()

    def revoke(self, case_id: str) -> None:
        self.db.execute("UPDATE edges SET revoked_at=? WHERE case_id=? AND rel='FLAGGED_IN'", (now(), case_id))
        self.db.commit()

    def commit(self) -> None:
        self.db.commit()

    # ---- queries (all exclude the current case and revoked edges: edge cases G-1, G-7) --------------
    def company_for_domain(self, domain: str | None) -> str | None:
        if not domain:
            return None
        r = self.db.execute("SELECT src FROM edges WHERE dst=? AND rel IN ('OWNS_DOMAIN','PAID_FROM_DOMAIN','ONBOARDED') "
                            "AND revoked_at IS NULL LIMIT 1", (f"domain:{domain}",)).fetchone()
        return r["src"] if r else None

    def known_domains_for_brand(self, brand_skeleton: str) -> set[str]:
        """Domains of vendors we already know (paid, onboarded or resolved) whose name label matches the brand."""
        rows = self.db.execute("SELECT DISTINCT dst FROM edges WHERE rel IN ('OWNS_DOMAIN','PAID_FROM_DOMAIN','ONBOARDED') "
                               "AND revoked_at IS NULL").fetchall()
        doms = {r["dst"].split(":", 1)[1] for r in rows}
        return {d for d in doms if geo.skeleton(geo.label_of(d)) == brand_skeleton}

    def history_domains(self, company: str | None, exclude_case: str = "") -> set[str]:
        if not company:
            return set()
        rows = self.db.execute("SELECT dst FROM edges WHERE src=? AND rel IN ('PAID_FROM_DOMAIN','ONBOARDED') "
                               "AND case_id!=? AND revoked_at IS NULL", (company, exclude_case)).fetchall()
        return {r["dst"].split(":", 1)[1] for r in rows}

    def vendor_known(self, company: str | None, exclude_case: str = "") -> bool:
        if not company:
            return False
        return self.db.execute("SELECT 1 FROM edges WHERE src=? AND rel='PAID' AND case_id!=? LIMIT 1",
                               (company, exclude_case)).fetchone() is not None

    def accounts_paid(self, company: str, exclude_case: str = "") -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT e.dst AS id, a.weak, a.last4, a.country, COUNT(*) AS n FROM edges e LEFT JOIN accounts a ON a.id=e.dst "
            "WHERE e.src=? AND e.rel='PAID' AND e.case_id!=? GROUP BY e.dst", (company, exclude_case)).fetchall()
        return {r["id"]: dict(r) for r in rows}

    def companies_paid(self, account_id: str, exclude_case: str = "") -> set[str]:
        rows = self.db.execute("SELECT DISTINCT src FROM edges WHERE dst=? AND rel='PAID' AND case_id!=?",
                               (account_id, exclude_case)).fetchall()
        return {r["src"] for r in rows}

    def cases_flagged(self, node_id: str, exclude_case: str = "") -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT e.case_id, e.citation, e.observed_at, c.data FROM edges e LEFT JOIN cases c ON c.case_id=e.case_id "
            "WHERE e.src=? AND e.rel='FLAGGED_IN' AND e.case_id!=? AND e.revoked_at IS NULL", (node_id, exclude_case)
        ).fetchall()

    def invoice_paid(self, company: str, invoice_no: str, exclude_case: str = ""):
        return self.db.execute(
            "SELECT p.*, a.weak FROM payments p LEFT JOIN accounts a ON a.id=p.account "
            "WHERE p.company=? AND UPPER(p.invoice_no)=UPPER(?) AND p.case_id!=? LIMIT 1",
            (company, invoice_no.strip(), exclude_case)
        ).fetchone()

    def payments_for(self, company: str, exclude_case: str = "") -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM payments WHERE company=? AND case_id!=? ORDER BY paid_on",
                               (company, exclude_case)).fetchall()

    def median_amount(self, company: str, currency: str, exclude_case: str = "") -> tuple[float | None, int]:
        vals = [r["amount"] for r in self.payments_for(company, exclude_case) if r["currency"] == currency]
        return (statistics.median(vals), len(vals)) if vals else (None, 0)

    # ---- cases ------------------------------------------------------------------------------------
    def save_case(self, case_id: str, decision: str, state: str, data: dict) -> None:
        t = now()
        self.db.execute("INSERT INTO cases VALUES (?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET decision=excluded.decision,"
                        " state=excluded.state, data=excluded.data, updated_at=excluded.updated_at",
                        (case_id, decision, state, json.dumps(data), t, t))
        self.db.commit()

    def get_case(self, case_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["data"] = json.loads(d["data"] or "{}")
        return d

    def all_cases(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM cases ORDER BY created_at").fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def set_state(self, case_id: str, state: str, data: dict | None = None) -> None:
        if data is not None:
            self.db.execute("UPDATE cases SET state=?, data=?, updated_at=? WHERE case_id=?",
                            (state, json.dumps(data), now(), case_id))
        else:
            self.db.execute("UPDATE cases SET state=?, updated_at=? WHERE case_id=?", (state, now(), case_id))
        self.db.commit()


def normalise_code(code: str | None) -> str | None:
    """SWIFT 'CHASUS33XXX' (primary office) == 'CHASUS33'; IFSC unchanged; uppercase, no spaces."""
    if not code:
        return None
    c = re.sub(r"\s", "", code).upper()
    if len(c) == 11 and c.endswith("XXX") and not geo.is_ifsc(c):
        c = c[:8]
    return c


def company_node(cid: int | None = None, domain: str | None = None) -> str | None:
    if cid is not None:
        return f"company:{cid}"
    return f"company:domain:{domain}" if domain else None


def seed(graph: Graph, client, path: Path | None = None) -> list[str]:
    """Load samples/seed_history.json: resolve each domain via Company Identify (free), write PAID history."""
    from .checks_company import identify
    from .evidence import first_match

    path = path or config.ROOT / "samples" / "seed_history.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    cite = f"seed:{path.relative_to(config.ROOT).as_posix()}"
    lines = []
    from .client import OfflineMiss
    for v in data["vendors"]:
        try:
            best, _ = first_match(identify(client, domain=v["domain"]))
        except OfflineMiss as e:   # edge case G-9: say what is missing and how to fix it
            raise RuntimeError(f"seed: cannot resolve {v['domain']}: offline and not in the cache. Run `python cli.py "
                               "seed` once online, or copy a filled .cache folder.") from e
        if not best:
            raise RuntimeError(f"seed: {v['domain']} did not resolve in Crustdata")
        cd = best["company_data"]
        comp = graph.node(company_node(cd["crustdata_company_id"]), "company", cd["basic_info"]["name"])
        for d in cd["basic_info"].get("all_domains") or [v["domain"]]:
            graph.edge(comp, "OWNS_DOMAIN", graph.node(f"domain:{d}", "domain", d),
                       f"crustdata:company/{cd['crustdata_company_id']}#basic_info.all_domains")
        a = v["account"]
        fp = graph.fingerprint(iban=a.get("iban"), account=a.get("account"), code=a.get("swift") or a.get("ifsc"))
        for p in v["payments"]:
            graph.record_payment(f"seed-{p['invoice_number']}", comp, p["invoice_number"], p["amount"], v["currency"],
                                 fp, p["paid_on"], cite, v["sender_domain"])
        lines.append(f"{cd['basic_info']['name']} ({comp}): {len(v['payments'])} payments to {fp.masked}")
    graph.commit()
    return lines
