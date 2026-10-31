"""PAYMENT family (spec section 7): is the money going where it should?

G1 account history · G2 account paid to another vendor · G3 account in a blocked case (hard)
G4 duplicate invoice · C9 amount anomaly · G5 several accounts in one email (E10).
C5 / C11 are Yaksh's (checks_company / checks_behaviour).

Every check is `check_X(inv, ctx, client, graph) -> list[Evidence]`; `check_payment` runs them all,
isolates crashes (S10) and then writes this case's idempotent verify-time edges (G-2).
"""
from __future__ import annotations

import logging
import re
import statistics
import tomllib
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Optional

from payeeguard.graph import (REPO_ROOT, Fingerprint, Graph, case_id_for, email_domain, graph_citation,
                              normalise_iban, normalise_invoice_no, short, vendor_node)
from payeeguard.models import Evidence, Invoice

log = logging.getLogger(__name__)

POLICY_PATH = REPO_ROOT / "policy.toml"
DEFAULT_POLICY: dict[str, Any] = {
    "decision": {"auto_pay_limit_usd": 5000},
    "fx_to_usd": {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012},
    "channel": {"free_mail": ["gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
                              "yahoo.com", "yahoo.co.in", "proton.me", "protonmail.com", "icloud.com",
                              "aol.com", "rediffmail.com", "zohomail.com", "gmx.com", "mail.com", "yandex.com"]},
    "payment": {"known_shared_accounts": [], "amount_anomaly_multiple": 3.0, "duplicate_window_days": 14},
    "signatory": {"payments_title_keywords": ["finance", "accounts receivable", "accounts payable", "accounting",
                                              "billing", "receivable", "payable", "controller", "treasury",
                                              "payments", "cfo", "collections"]},
    "buyer": {"buyer_domain": ""},
}


@lru_cache(maxsize=4)
def _load_policy(path: str) -> dict[str, Any]:
    merged = {k: dict(v) for k, v in DEFAULT_POLICY.items()}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("policy.toml unreadable (%s); using built-in defaults", exc)   # M7
        return merged
    for section, values in data.items():
        if isinstance(values, dict):
            merged.setdefault(section, {}).update(values)
    return merged


def policy(ctx: Optional[dict] = None) -> dict[str, Any]:
    """The pipeline may pass its own policy in ctx["policy"]; otherwise read policy.toml (M7 defaults)."""
    if ctx and isinstance(ctx.get("policy"), dict):
        return ctx["policy"]
    return _load_policy(str(POLICY_PATH))


# ---------------------------------------------------------------- shared helpers

def _ev(check: str, claim: str, status: str, detail: str, source: str, weight: int = 0,
        hard: bool = False, reason: Optional[str] = None, confirms: Optional[str] = None) -> Evidence:
    return Evidence(check=check, family="PAYMENT", claim=claim, status=status,  # type: ignore[arg-type]
                    detail=detail, source=source, weight=weight if status == "contradicted" else 0,
                    hard_block=hard and status == "contradicted",
                    unchecked_reason=reason if status == "unchecked" else None,
                    confirms=confirms if status == "verified" else None)  # type: ignore[arg-type]


def _case(inv: Invoice, ctx: dict) -> str:
    if not ctx.get("case_id"):
        ctx["case_id"] = case_id_for(inv.raw_text)
    return ctx["case_id"]


def _vendor(inv: Invoice, ctx: dict) -> Optional[str]:
    if "vendor_node" not in ctx:
        ctx["vendor_node"] = vendor_node(ctx.get("company_id"), inv.vendor_domain)
    return ctx["vendor_node"]


def _fp(inv: Invoice, ctx: dict, graph: Graph) -> Fingerprint:
    if not isinstance(ctx.get("fingerprint"), Fingerprint):
        ctx["fingerprint"] = graph.fingerprint_invoice(inv)
    return ctx["fingerprint"]


def _vendor_name(inv: Invoice, ctx: dict, graph: Graph, vendor: Optional[str]) -> str:
    company = ctx.get("company") or {}
    name = (company.get("basic_info") or {}).get("name") if isinstance(company, dict) else None
    return name or inv.vendor_name or (graph.node_label(vendor) if vendor else "") or "the vendor"


def _labels(history: list[dict]) -> str:
    labels = sorted({p["account_label"] for p in history if p.get("account_label")})
    return ", ".join(labels) or "other accounts"


def _paid_citation(vendor: str, account: str, history: list[dict]) -> str:
    cases = sorted({p["case_id"] for p in history if account in p["account_ids"]})
    return graph_citation("PAID", vendor, account, cases[0] if cases else "history")


def _same_account(fp: Fingerprint, other_ids: Any) -> bool:
    """Does a recorded account (its id list) match this invoice's account?
    Strong ids decide; a code-less alias counts unless both sides name different banks;
    a weak id counts only when the invoice has nothing better (G-5)."""
    ids = set(other_ids)
    if fp.matchable:
        if set(fp.strong) & ids:
            return True
        return bool(fp.alias and fp.alias in ids and not fp.alias_conflict(ids))
    return bool(fp.weak and fp.weak in ids)


def _no_graph(check: str, claim: str) -> list[Evidence]:
    return [_ev(check, claim, "unchecked", "payment-trust graph unavailable", "graph:unavailable",
                reason="api_error")]


def _to_usd(amount: float, currency: Optional[str], fx: dict) -> Optional[float]:
    rate = fx.get((currency or "").upper())
    return amount * float(rate) if rate is not None else None


def _parse_date(value: Optional[str]) -> Optional[date]:
    s = (value or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _today(ctx: dict) -> date:
    now = ctx.get("now")
    if isinstance(now, datetime):
        return now.date()
    if isinstance(now, date):
        return now
    return _parse_date(now) or datetime.now(timezone.utc).date()


# ---------------------------------------------------------------- G1

G1 = "G1_account_history"
G1_CLAIM = "Account matches this vendor's payment history"


def check_g1_account_history(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    if graph is None:
        return _no_graph(G1, G1_CLAIM)
    case, vendor, fp = _case(inv, ctx), _vendor(inv, ctx), _fp(inv, ctx, graph)
    history = graph.payments(vendor, case)
    paid = set(graph.accounts_paid(vendor, case))
    ctx["vendor_known"] = bool(paid)
    ctx["payment_history"] = len(history)
    name = _vendor_name(inv, ctx, graph, vendor)
    if not vendor:
        return [_ev(G1, G1_CLAIM, "unchecked", "vendor not identified, so no history to compare",
                    "graph:no_vendor", reason="no_data")]
    if fp.empty:
        if inv.bank_change_requested:
            return [_ev(G1, G1_CLAIM, "unchecked", "bank change requested but no new account given (S5)",
                        "extract:regex#bank_account", reason="not_applicable")]
        if not paid:
            return [_ev(G1, G1_CLAIM, "unchecked", f"no account in the email and no payment history for {name}",
                        f"graph:PAID/{vendor}-> none", reason="no_data")]
        last = history[-1] if history else None
        ctx["ledger_account"] = last["account_label"] if last else None   # E11: pay the ledger account
        acc = (last["account_ids"][0] if last else sorted(paid)[0])
        return [_ev(G1, G1_CLAIM, "verified",
                    f"no account in the email: the ledger account on file ({ctx['ledger_account'] or 'account'}, "
                    f"{len(history)} prior payments) will be used",
                    _paid_citation(vendor, acc, history), confirms="money")]
    if not paid:
        return [_ev(G1, G1_CLAIM, "unchecked", f"first-time payee: no payment history for {name}",
                    f"graph:PAID/{vendor}-> none", reason="no_data")]
    hits = [p for p in history if _same_account(fp, p["account_ids"])]
    if hits:
        acc = next(a for a in fp.ids if a in hits[0]["account_ids"])
        weak = "" if fp.matchable else " (weak match: bank code + last 4 only)"
        return [_ev(G1, G1_CLAIM, "verified",
                    f"{fp.label} was paid {len(hits)} time(s) before by {name}{weak}",
                    _paid_citation(vendor, acc, history), confirms="money")]
    return [_ev(G1, G1_CLAIM, "contradicted",
                f"{name} has {len(history)} prior payment(s), all to {_labels(history)}; never to {fp.label}",
                graph_citation("PAID", vendor, sorted(paid)[0], "history"), weight=25)]


# ---------------------------------------------------------------- G2

G2 = "G2_account_other_vendor"
G2_CLAIM = "Account is not paid to a different vendor"


def _allow_listed(fp: Fingerprint, ctx: dict) -> bool:
    allowed = {a if a.startswith("account:") else f"account:{a}"
               for a in policy(ctx)["payment"].get("known_shared_accounts", []) if isinstance(a, str) and a}
    return any(a in allowed for a in fp.matchable)


def check_g2_account_other_vendor(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    if graph is None:
        return _no_graph(G2, G2_CLAIM)
    case, vendor, fp = _case(inv, ctx), _vendor(inv, ctx), _fp(inv, ctx, graph)
    if fp.empty:
        return [_ev(G2, G2_CLAIM, "unchecked", "no account in the email", "extract:regex#bank_account",
                    reason="not_applicable")]
    if not fp.matchable:
        return [_ev(G2, G2_CLAIM, "unchecked", "only a weak fingerprint (bank code + last 4): never used for G2",
                    "graph:weak_fingerprint", reason="not_applicable")]
    others = [c for c in graph.companies_paid(fp.matchable, case) if c != vendor
              and any(_same_account(fp, p["account_ids"]) for p in graph.payments(c, case))]
    if not others:
        return [_ev(G2, G2_CLAIM, "verified", f"{fp.label} has not been paid to any other vendor",
                    f"graph:PAID/*->{short(fp.matchable[0])} -> none")]
    names = ", ".join(graph.node_label(o) or o for o in others)
    acc = next(a for a in fp.matchable if a in set(graph.accounts_paid(others[0], case)))
    cite = graph_citation("PAID", others[0], acc, "history")
    if _allow_listed(fp, ctx):
        return [_ev(G2, G2_CLAIM, "verified",
                    f"{fp.label} is also paid to {names}, but it is on the known_shared_accounts allow-list", cite)]
    return [_ev(G2, G2_CLAIM, "contradicted",
                f"{fp.label} was paid to a different vendor: {names} (factoring, aggregator or mule?)",
                cite, weight=30)]


# ---------------------------------------------------------------- G3

G3 = "G3_account_flagged"
G3_CLAIM = "Account is not linked to a blocked case"


def check_g3_account_flagged(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    if graph is None:
        return _no_graph(G3, G3_CLAIM)
    case, fp = _case(inv, ctx), _fp(inv, ctx, graph)
    if fp.empty:
        return [_ev(G3, G3_CLAIM, "unchecked", "no account in the email", "extract:regex#bank_account",
                    reason="not_applicable")]
    if not fp.matchable:
        return [_ev(G3, G3_CLAIM, "unchecked", "only a weak fingerprint (bank code + last 4): never used for G3",
                    "graph:weak_fingerprint", reason="not_applicable")]
    flagged = []
    for f in graph.cases_flagged(fp.matchable, case):
        case_accounts = [e["src"] for e in graph.edges_for_case(f["case_id"])
                         if e["rel"] == "FLAGGED_IN" and e["src"].startswith("account:") and not e["revoked_at"]]
        if _same_account(fp, case_accounts):
            flagged.append(f)
    if not flagged:
        return [_ev(G3, G3_CLAIM, "verified", f"{fp.label} does not appear in any blocked case",
                    f"graph:FLAGGED_IN/{short(fp.matchable[0])}-> none")]
    strong_hits = [f for f in flagged if f["src"] in fp.strong]
    cases = list(dict.fromkeys(f["case_id"] for f in flagged))
    ctx["related_cases"] = cases
    first = (strong_hits or flagged)[0]
    what = ", ".join(f"case {c[:4]} ({next(f['label'] for f in flagged if f['case_id'] == c) or 'BLOCKED'})"
                     for c in cases)
    cite = graph_citation("FLAGGED_IN", first["src"], first["dst"], first["case_id"])
    if not strong_hits:      # same number but no bank code on one side: two banks can share a number
        return [_ev(G3, G3_CLAIM, "contradicted",
                    f"the account number {fp.label} was used in {what}; bank code missing, so not a hard fact",
                    cite, weight=30)]
    return [_ev(G3, G3_CLAIM, "contradicted", f"{fp.label} was used in {what}, first flagged {first['observed_at']}",
                cite, hard=True)]


# ---------------------------------------------------------------- G4

G4 = "G4_duplicate"
G4_CLAIM = "Invoice has not already been paid"


def check_g4_duplicate(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    if graph is None:
        return _no_graph(G4, G4_CLAIM)
    case, vendor, fp = _case(inv, ctx), _vendor(inv, ctx), _fp(inv, ctx, graph)
    if not vendor:
        return [_ev(G4, G4_CLAIM, "unchecked", "vendor not identified", "graph:no_vendor", reason="no_data")]
    no = normalise_invoice_no(inv.invoice_number)
    if no:
        paid = graph.invoice_paid(vendor, no, case)
        if not paid:
            return [_ev(G4, G4_CLAIM, "verified", f"invoice {no} has not been paid before",
                        f"graph:invoice_paid/{vendor}#{no} -> none")]
        p = paid[-1]
        cite = graph_citation("PAID", vendor, p["account_ids"][0] if p["account_ids"] else "account", p["case_id"])
        when = f"invoice {no} was already paid on {p['paid_on']} ({p['currency'] or ''} {p['amount'] or ''})".strip()
        if fp.empty or any(_same_account(fp, q["account_ids"]) for q in paid):
            return [_ev(G4, G4_CLAIM, "contradicted", f"{when} to the same account {p['account_label']}",
                        cite, weight=40)]
        if not fp.strong:     # weak / code-less mismatch: different, but never a hard fact (G-5)
            return [_ev(G4, G4_CLAIM, "contradicted",
                        f"{when} to {p['account_label']}; this request names {fp.label} (weak fingerprint)",
                        cite, weight=40)]
        return [_ev(G4, G4_CLAIM, "contradicted",
                    f"{when} to {p['account_label']}; this request re-sends it to a different account {fp.label}",
                    cite, hard=True)]
    if inv.amount is None or inv.amount <= 0:
        return [_ev(G4, G4_CLAIM, "unchecked", "no invoice number and no amount to compare",
                    "extract:regex#invoice_number", reason="not_applicable")]
    window = int(policy(ctx)["payment"].get("duplicate_window_days", 14))
    ref = _parse_date(inv.invoice_date) or _today(ctx)
    cur = (inv.currency or "").upper()
    for p in graph.payments(vendor, case):
        d = _parse_date(p["paid_on"])
        if (p["amount"] and d and (p["currency"] or "") == cur and abs(p["amount"] - inv.amount) <= 0.01 * inv.amount
                and abs((ref - d).days) <= window):
            return [_ev(G4, G4_CLAIM, "contradicted",
                        f"no invoice number; {cur} {inv.amount:,.2f} matches a payment of {p['amount']:,.2f} "
                        f"on {p['paid_on']} (within {window} days)",
                        graph_citation("PAID", vendor, p["account_ids"][0], p["case_id"]), weight=15)]
    return [_ev(G4, G4_CLAIM, "verified", f"no invoice number; no payment of a similar amount within {window} days",
                f"graph:payments/{vendor} -> none")]


# ---------------------------------------------------------------- C9

C9 = "C9_amount"
C9_CLAIM = "Amount is in line with this vendor's history"


def check_c9_amount(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    if graph is None:
        return _no_graph(C9, C9_CLAIM)
    case, vendor = _case(inv, ctx), _vendor(inv, ctx)
    if inv.amount is None or inv.amount <= 0:
        return [_ev(C9, C9_CLAIM, "unchecked", "no positive amount to compare", "extract:regex#amount",
                    reason="not_applicable")]
    pol = policy(ctx)
    fx = pol.get("fx_to_usd", {})
    multiple = float(pol["payment"].get("amount_anomaly_multiple", 3.0))
    history = graph.payments(vendor, case)
    cur = (inv.currency or "").upper()
    same = [p["amount"] for p in history if p["amount"] and (p["currency"] or "") == cur]
    if len(same) >= 3 and cur:
        prior, amount, unit = same, inv.amount, cur
    else:
        usd = _to_usd(inv.amount, cur, fx)
        prior = [u for p in history if p["amount"] for u in [_to_usd(p["amount"], p["currency"], fx)] if u]
        amount, unit = usd, "USD"
        if usd is None and history:
            return [_ev(C9, C9_CLAIM, "unchecked", f"currency {cur or 'unknown'} can't be compared to history",
                        "policy:fx_to_usd", reason="no_data")]
    if len(prior) < 3 or amount is None:
        return [_ev(C9, C9_CLAIM, "unchecked", f"only {len(prior)} prior payment(s); need 3 to compare",
                    f"graph:payments/{vendor or 'unknown'}", reason="no_data")]
    median = statistics.median(prior)
    detail = f"{unit} {amount:,.2f} vs median {unit} {median:,.2f} of {len(prior)} prior payments"
    cite = f"graph:payments/{vendor}#median"
    if amount > multiple * median:
        return [_ev(C9, C9_CLAIM, "contradicted", f"{detail}: more than {multiple:g}x", cite, weight=10)]
    return [_ev(C9, C9_CLAIM, "verified", f"{detail}: within {multiple:g}x", cite)]


# ---------------------------------------------------------------- G5 (E10)

G5 = "G5_ambiguous_payment"
G5_CLAIM = "The email names one payment account"

_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ \-]?[A-Z0-9]{4}){2,7}(?:[ \-]?[A-Z0-9]{1,3})?\b", re.I)
_ACCT_RE = re.compile(r"\b(?:account|acct|a/c)(?:\s*(?:no\.?|number|num|#))?\s*[:#\-]?\s*([0-9][0-9 \-]{4,22}[0-9])",
                      re.I)
_WROTE_RE = re.compile(r"^\s*(on .{3,200}wrote:|-{2,}\s*original message\s*-{2,})\s*$", re.I)


def iban_valid(iban: str) -> bool:
    s = normalise_iban(iban) or ""
    if not 15 <= len(s) <= 34 or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    rearranged = s[4:] + s[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


def _current_text(raw: str) -> str:
    """Drop quoted history (E4): '>' lines and everything after 'On … wrote:' / 'Original Message'."""
    keep = []
    for line in (raw or "").splitlines():
        if _WROTE_RE.match(line):
            break
        if not line.lstrip().startswith(">"):
            keep.append(line)
    return "\n".join(keep)


def accounts_in_text(raw: str) -> list[dict]:
    text = _current_text(raw)
    found: list[dict] = []
    for m in _IBAN_RE.finditer(text):
        if iban_valid(m.group(0)):
            found.append({"iban": m.group(0)})
    for m in _ACCT_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if 6 <= len(digits) <= 18:
            found.append({"account": digits})
    return found


def _distinct_accounts(cands: list[dict], graph: Graph) -> list[Fingerprint]:
    groups: list[tuple[set[str], str, str, Fingerprint]] = []   # ids, iban, account, fp
    for c in cands:
        fp = graph.fingerprint(c.get("iban"), c.get("account"), c.get("swift") or c.get("code"), c.get("last4"))
        if fp.empty:
            continue
        iban, acct = normalise_iban(c.get("iban")) or "", re.sub(r"\D", "", c.get("account") or "")
        for g in groups:
            same_ids = bool(g[0] & set(fp.matchable or fp.ids))
            inside = ((acct and len(acct) >= 6 and g[1].endswith(acct))          # account inside an IBAN
                      or (g[2] and len(g[2]) >= 6 and iban.endswith(g[2])))
            if same_ids or inside:
                g[0].update(fp.ids)
                break
        else:
            groups.append((set(fp.ids), iban, acct, fp))
    return [g[3] for g in groups]


def check_g5_ambiguous_payment(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    if graph is None:
        return _no_graph(G5, G5_CLAIM)
    cands = [a for a in ctx.get("accounts") or [] if isinstance(a, dict)]
    cands += accounts_in_text(inv.raw_text)
    if inv.bank_iban or inv.bank_account:
        cands.insert(0, {"iban": inv.bank_iban, "account": inv.bank_account, "swift": inv.bank_swift})
    distinct = _distinct_accounts(cands, graph)
    if len(distinct) >= 2:
        return [_ev(G5, G5_CLAIM, "contradicted",
                    f"two different accounts given: {', '.join(fp.label for fp in distinct)}",
                    "extract:regex#bank_account", weight=25)]
    if not distinct:
        return [_ev(G5, G5_CLAIM, "unchecked", "no account in the email", "extract:regex#bank_account",
                    reason="not_applicable")]
    return [_ev(G5, G5_CLAIM, "verified", f"one account given: {distinct[0].label}", "extract:regex#bank_account")]


# ---------------------------------------------------------------- runner

CHECKS: list[tuple[str, str, Callable[..., list[Evidence]]]] = [
    (G1, G1_CLAIM, check_g1_account_history),
    (G2, G2_CLAIM, check_g2_account_other_vendor),
    (G3, G3_CLAIM, check_g3_account_flagged),
    (G4, G4_CLAIM, check_g4_duplicate),
    (C9, C9_CLAIM, check_c9_amount),
    (G5, G5_CLAIM, check_g5_ambiguous_payment),
]


def check_payment(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    """All PAYMENT checks; one crash becomes unchecked/api_error (S10). Then writes this case's
    SENT / CLAIMS_VENDOR / PAYS_TO edges (idempotent, G-2) and fills ctx["graph_edges"]."""
    out: list[Evidence] = []
    for check, claim, fn in CHECKS:
        try:
            out += fn(inv, ctx, client, graph)
        except Exception as exc:
            log.error("%s crashed: %s", check, type(exc).__name__)
            out.append(_ev(check, claim, "unchecked", f"check failed ({type(exc).__name__})",
                           "payeeguard:check_error", reason="api_error"))
    if graph is not None:
        try:
            case, vendor = _case(inv, ctx), _vendor(inv, ctx)
            graph.record_invoice(case, inv, vendor, _fp(inv, ctx, graph))
            ctx["graph_edges"] = graph.money_trail(case)
        except Exception as exc:
            log.error("graph write failed: %s", type(exc).__name__)
    return out


def sender_domain(inv: Invoice) -> Optional[str]:
    """Convenience for the gate's PAID_FROM_DOMAIN write."""
    return email_domain(inv.sender_email)
