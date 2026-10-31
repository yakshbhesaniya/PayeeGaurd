"""Adapters to teammates' modules (A: client + checks, B: graph + audit) with stand-ins until they merge.

Everything Bahul's code needs from A and B goes through this file, so a merge only ever touches here.

Graph interface used by pipeline / gate / report (Aditya's graph.py should provide these names):
    add_edge(src, rel, dst, citation, case_id) -> None        idempotent (UNIQUE src,rel,dst,case_id)
    account_node(inv) -> str | None                           "account:<hmac>" or "account:weak:<sha256>"
    accounts_paid(company_node, exclude_case=None) -> dict[str, int]   account node -> number of PAID edges
    cases_flagged(node, exclude_case=None) -> list[str]       case ids with FLAGGED_IN from node (not revoked)
    revoke_case_flags(case_id, at) -> int                     G-7 override
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import inspect
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .models import Evidence, Invoice

log = logging.getLogger(__name__)

CHECK_MODULES = [  # order matters: C1 fills ctx["company"] for the others
    ("checks_company", "COMPANY"),
    ("checks_channel", "CHANNEL"),
    ("checks_signatory", "SIGNATORY"),
    ("checks_payment", "PAYMENT"),
    ("checks_behaviour", "BEHAVIOUR"),
]

CheckFn = Callable[[Invoice, dict, Any, Any], list[Evidence]]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _module(name: str):
    try:
        return importlib.import_module(f"payeeguard.{name}")
    except ImportError as e:
        if getattr(e, "name", "") in (f"payeeguard.{name}", name):
            return None
        raise


def load_checks() -> list[tuple[str, str, CheckFn]]:
    """(name, family, fn) for every check_* function, in module then source order."""
    out = []
    for mod_name, family in CHECK_MODULES:
        mod = _module(mod_name)
        if mod is None:
            continue
        fns = [(n, f) for n, f in inspect.getmembers(mod, inspect.isfunction)
               if n.startswith("check_") and f.__module__ == mod.__name__]
        fns.sort(key=lambda nf: nf[1].__code__.co_firstlineno)
        out += [(n, family, f) for n, f in fns]
    return out


def checks_available() -> bool:
    return bool(load_checks())


def make_client() -> Any:
    """Yaksh's Crustdata client, or None before merge (checks then return unchecked rows)."""
    mod = _module("client")
    if mod is None:
        return None
    for name in ("CrustdataClient", "Client"):
        cls = getattr(mod, name, None)
        if cls is not None:
            return cls()
    return getattr(mod, "client", None) or mod


def client_counters(client: Any) -> tuple[int, float]:
    if client is None:
        return 0, 0.0
    calls = getattr(client, "calls", 0)
    credits = getattr(client, "credits", 0.0)
    return int(calls() if callable(calls) else calls or 0), float(credits() if callable(credits) else credits or 0.0)


def onboard_facts_fn() -> Optional[Callable]:
    mod = _module("checks_company")
    return getattr(mod, "onboard_facts", None) if mod else None


def open_graph(path: Optional[str] = None) -> Any:
    mod = _module("graph")
    if mod is not None:
        for name in ("Graph", "PaymentGraph", "TrustGraph"):
            cls = getattr(mod, name, None)
            if cls is not None:
                return cls(path) if path else cls()
        if hasattr(mod, "open_graph"):
            return mod.open_graph(path)
    return MemoryGraph()


def seed_graph(graph: Any, seed_path: str, client: Any) -> str:
    for mod_name in ("graph", "seed"):
        mod = _module(mod_name)
        for fn in ("load_seed", "seed", "seed_from_file"):
            if mod is not None and hasattr(mod, fn):
                getattr(mod, fn)(graph, seed_path, client)
                return f"seeded from {seed_path}"
    if isinstance(graph, MemoryGraph):
        return graph.load_seed(seed_path)
    raise RuntimeError("no seed loader available (graph.load_seed missing)")


def audit_append(event: dict[str, Any]) -> Optional[str]:
    mod = _module("audit")
    for fn in ("append", "append_event", "log_event", "record"):
        if mod is not None and hasattr(mod, fn):
            try:
                return getattr(mod, fn)(event)
            except Exception:  # audit must never break the decision path
                log.exception("audit append failed")
                return None
    return None


# ---------- stand-in graph (in memory) until graph.py merges ----------

def fp_secret() -> bytes:
    secret = os.environ.get("PAYEEGUARD_FP_SECRET", "")
    if not secret:
        raise RuntimeError("PAYEEGUARD_FP_SECRET missing")   # G-11: never use an insecure default
    return secret.encode()


def fingerprint(inv: Invoice) -> Optional[str]:
    """Spec 8: HMAC of the full account; weak sha256(SWIFT + last4) when only the last 4 digits are known."""
    swift = (inv.bank_swift or "").upper()
    if inv.bank_iban:
        norm = inv.bank_iban.replace(" ", "").upper()
    elif inv.bank_account:
        norm = f"{swift}:{inv.bank_account.replace(' ', '').replace('-', '')}"
    elif inv.bank_account_last4:
        return "account:weak:" + hashlib.sha256((swift + inv.bank_account_last4).encode()).hexdigest()[:16]
    else:
        return None
    return "account:" + hmac.new(fp_secret(), norm.encode(), hashlib.sha256).hexdigest()[:16]


class MemoryGraph:
    """Minimal in-memory graph with the interface above. Not persistent across processes."""

    def __init__(self) -> None:
        self.edges: list[dict[str, Any]] = []

    def add_edge(self, src: str, rel: str, dst: str, citation: str, case_id: str = "") -> None:
        key = (src, rel, dst, case_id)
        if not any((e["src"], e["rel"], e["dst"], e["case_id"]) == key for e in self.edges):
            self.edges.append({"src": src, "rel": rel, "dst": dst, "citation": citation, "case_id": case_id,
                               "observed_at": now(), "revoked_at": None})

    def account_node(self, inv: Invoice) -> Optional[str]:
        return fingerprint(inv)

    def _live(self, exclude_case: Optional[str]):
        return [e for e in self.edges if not e["revoked_at"] and (not exclude_case or e["case_id"] != exclude_case)]

    def accounts_paid(self, company_node: str, exclude_case: Optional[str] = None) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self._live(exclude_case):
            if e["rel"] == "PAID" and e["src"] == company_node:
                out[e["dst"]] = out.get(e["dst"], 0) + 1
        return out

    def cases_flagged(self, node: str, exclude_case: Optional[str] = None) -> list[str]:
        return sorted({e["dst"].removeprefix("case:") for e in self._live(exclude_case)
                       if e["rel"] == "FLAGGED_IN" and e["src"] == node})

    def revoke_case_flags(self, case_id: str, at: Optional[str] = None) -> int:
        n = 0
        for e in self.edges:
            if e["rel"] == "FLAGGED_IN" and e["case_id"] == case_id and not e["revoked_at"]:
                e["revoked_at"] = at or now()
                n += 1
        return n

    def load_seed(self, path: str) -> str:
        import json
        with open(path, encoding="utf-8") as f:
            seed = json.load(f)
        known = {"freshworks.com": 6042422, "zoho.com": 1032449}   # from spec 6.2 (stand-in only)
        for v in seed["vendors"]:
            company = f"company:{known.get(v['domain'], v['domain'])}"
            a = v["account"]
            inv = Invoice(None, None, None, None, None, None, None, None, None,
                          bank_account=a["account"], bank_swift=a.get("swift") or a.get("ifsc"))
            acct = fingerprint(inv)
            for p in v["payments"]:
                case = f"seed-{v['domain']}-{p['invoice_number']}"
                self.add_edge(company, "PAID", acct, "seed:samples/seed_history.json", case)
                self.add_edge(company, "PAID_FROM_DOMAIN", f"domain:{v['sender_domain']}",
                              "seed:samples/seed_history.json", case)
        return f"seeded {len(seed['vendors'])} vendors (in-memory stand-in graph)"
