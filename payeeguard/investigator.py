"""Fraud Pattern Investigator (spec 9): deterministic patterns over Evidence -> hypothesis + ruled-out list.

Narratives are templates filled only with Evidence.detail text, so every sentence traces to a cited row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .models import Evidence, Hypothesis, Invoice, RuledOut
from .scoring import is_check

Rows = list[Evidence]


# ---------- fact helpers ----------

def _rows(ev: Rows, prefix: str, status: Optional[str] = None) -> Rows:
    return [e for e in ev if is_check(e, prefix) and (status is None or e.status == status)]


def _contra(ev: Rows, prefix: str) -> Rows:
    return _rows(ev, prefix, "contradicted")


def _lookalike(ev: Rows) -> Rows:
    return [e for e in ev if e.status == "contradicted" and any(is_check(e, p) for p in ("C2", "C2c", "C10"))
            and (e.hard_block or "lookalike" in e.detail.lower())]


def _c3(ev: Rows, kind: str) -> Rows:
    keys = {"ex": ("ex-employee", "former employee", "left "), "role": ("role authority", "role-authority")}[kind]
    weight = {"ex": 35, "role": 20}[kind]
    return [e for e in _contra(ev, "C3") if any(k in e.detail.lower() for k in keys) or e.weight == weight]


def _tactic(ev: Rows, name: str) -> Rows:
    human = name.replace("_", " ")
    return [e for e in _contra(ev, "C14") if name in e.check or name in e.detail.lower() or human in e.detail.lower()]


def _detail(rows: Rows) -> str:
    return rows[0].detail.rstrip(".") if rows else ""


@dataclass
class _Match:
    attack_type: str
    title: str
    supporting: Rows
    required: Rows


@dataclass
class _Pattern:
    attack_type: str
    title: str
    block_grade: bool
    required: Callable[[Rows, Invoice], Optional[Rows]]     # rows proving the pattern, None = no match
    supporting: Callable[[Rows, Invoice], Rows]
    ruled_out: Callable[[Rows], Rows]                       # rows proving it did NOT happen


def _bank_change(inv: Invoice) -> Rows:
    if not inv.bank_change_requested:
        return []
    return [Evidence("bank_change", "CONTEXT", "Payment details unchanged", "contradicted",
                     "the email asks to change bank details", "extract:regex#bank_change_requested")]


def _req_mule(ev: Rows, inv: Invoice) -> Optional[Rows]:
    return _contra(ev, "G3") or None


def _req_impersonation(ev: Rows, inv: Invoice) -> Optional[Rows]:
    return _lookalike(ev) or None


def _req_compromised(ev: Rows, inv: Invoice) -> Optional[Rows]:
    c2 = _rows(ev, "C2", "verified")
    money = _contra(ev, "G1") + _contra(ev, "G3") + _contra(ev, "C10")
    return c2 + money if c2 and money else None


def _req_ex(ev: Rows, inv: Invoice) -> Optional[Rows]:
    return _c3(ev, "ex") or None


def _req_role(ev: Rows, inv: Invoice) -> Optional[Rows]:
    return _c3(ev, "role") or None


def _req_unverified(ev: Rows, inv: Invoice) -> Optional[Rows]:
    c2b = _contra(ev, "C2b")
    return c2b if c2b and not _rows(ev, "C3", "verified") else None


def _req_unverifiable(ev: Rows, inv: Invoice) -> Optional[Rows]:
    rows = [e for e in _rows(ev, "C1", "unchecked") if e.unchecked_reason == "no_data"]
    return rows or None


PATTERNS: list[_Pattern] = [
    _Pattern("mule_campaign", "Account linked to a prior blocked attack", True, _req_mule,
             lambda ev, inv: _contra(ev, "G1") + _contra(ev, "G2"),
             lambda ev: _rows(ev, "G1", "verified")),
    _Pattern("vendor_impersonation", "Lookalike-domain impersonation", True, _req_impersonation,
             lambda ev, inv: _contra(ev, "C8") + _contra(ev, "C5") + _contra(ev, "C14"),
             lambda ev: _rows(ev, "C2", "verified") if not _contra(ev, "C10") else []),
    _Pattern("compromised_vendor_mailbox", "Real vendor mailbox, new payment details", False, _req_compromised,
             lambda ev, inv: _bank_change(inv) + _contra(ev, "C14") + _contra(ev, "C5"),
             lambda ev: _rows(ev, "G1", "verified")),
    _Pattern("duplicate_replay", "Already-paid invoice re-sent", False,
             lambda ev, inv: _contra(ev, "G4") or None,
             lambda ev, inv: _contra(ev, "G1"), lambda ev: []),
    _Pattern("ex_employee", "Former employee requesting payment", False, _req_ex,
             lambda ev, inv: _contra(ev, "C2b") + _contra(ev, "G1"),
             lambda ev: _rows(ev, "C3", "verified")),
    _Pattern("payment_method_fraud", "Off-rails payment request", False,
             lambda ev, inv: _tactic(ev, "payment_method") or None,
             lambda ev, inv: [], lambda ev: []),
    _Pattern("unauthorized_change", "Real employee outside payments role", False, _req_role,
             lambda ev, inv: _contra(ev, "G1"), lambda ev: []),
    _Pattern("unverified_signatory", "Unverified person on a personal mailbox", False, _req_unverified,
             lambda ev, inv: _tactic(ev, "channel_switch") + _bank_change(inv),
             lambda ev: _rows(ev, "C3", "verified")),
    _Pattern("unverifiable_vendor", "Vendor we can't verify (not proof of fraud)", False, _req_unverifiable,
             lambda ev, inv: _contra(ev, "C4") + _contra(ev, "C8") + _contra(ev, "C2b"),
             lambda ev: _rows(ev, "C1", "verified")),
]
INDEX = {p.attack_type: i for i, p in enumerate(PATTERNS)}

RULED_OUT_LABEL = {
    "mule_campaign": "mule account",
    "vendor_impersonation": "lookalike domain",
    "compromised_vendor_mailbox": "compromised mailbox",
    "ex_employee": "ex-employee",
    "unverified_signatory": "unverified signatory",
    "unverifiable_vendor": "fake vendor",
}


# ---------- narratives ----------

def _narrative(attack: str, ev: Rows, inv: Invoice, also: list[str]) -> str:
    vendor = inv.vendor_name or "the vendor"
    c2 = _detail(_rows(ev, "C2", "verified"))
    if attack == "mule_campaign":
        lead = f"The email comes from {vendor}'s real domain ({c2}), but asks" if c2 else "The email asks"
        parts = [f"{lead} for payment to an account linked to a previously blocked case ({_detail(_contra(ev, 'G3'))})."]
        if _contra(ev, "G1"):
            parts.append(f"Payment history: {_detail(_contra(ev, 'G1'))}.")
        tail = "a compromised vendor mailbox used in a mule-account campaign" if "compromised_vendor_mailbox" in also \
            else "a mule-account campaign"
        parts.append(f"This pattern indicates {tail}.")
        return " ".join(parts)
    if attack == "vendor_impersonation":
        parts = [f"The sender's domain is a lookalike: {_detail(_lookalike(ev))}."]
        for p in ("C8", "C5"):
            if _contra(ev, p):
                parts.append(f"{_detail(_contra(ev, p))}.")
        parts.append(f"This pattern indicates someone impersonating {vendor}.")
        return " ".join(parts)
    if attack == "compromised_vendor_mailbox":
        money = _contra(ev, "G3") or _contra(ev, "G1") or _contra(ev, "C10")
        return (f"The email comes from {vendor}'s real domain ({c2}), but {_detail(money)}. "
                "This pattern indicates a compromised vendor mailbox; confirm by calling the vendor's official number.")
    if attack == "duplicate_replay":
        return f"{_detail(_contra(ev, 'G4'))}. This invoice appears to have been paid already and is being re-sent."
    if attack == "ex_employee":
        return f"{_detail(_c3(ev, 'ex'))}. A former employee is requesting payment."
    if attack == "unauthorized_change":
        return f"{_detail(_c3(ev, 'role'))}. A real employee outside a payments role is asking to change payment details."
    if attack == "payment_method_fraud":
        return f"{_detail(_tactic(ev, 'payment_method'))}. Payment is requested off normal banking rails."
    if attack == "unverified_signatory":
        c3 = _detail(_rows(ev, "C3")) or "no matching person record"
        return (f"The request comes from a personal mailbox ({_detail(_contra(ev, 'C2b'))}) and the sender could not be "
                f"verified as working at {vendor} ({c3}). This is unverified, not proof of fraud.")
    if attack == "unverifiable_vendor":
        return (f"The vendor could not be verified ({_detail(_rows(ev, 'C1'))}). This is not proof of fraud: "
                "the vendor needs onboarding (KYB) checks before payment.")
    return ""


def investigate(evidence: Rows, inv: Invoice, low_confidence: bool = False
                ) -> tuple[Hypothesis, list[Hypothesis], list[RuledOut]]:
    matches: list[_Match] = []
    ruled: list[RuledOut] = []
    for p in PATTERNS:
        req = p.required(evidence, inv)
        if req is not None:
            matches.append(_Match(p.attack_type, p.title, p.supporting(evidence, inv), req))
            continue
        proof = p.ruled_out(evidence)
        if proof and p.attack_type in RULED_OUT_LABEL:
            ruled.append(RuledOut(p.attack_type, f"{RULED_OUT_LABEL[p.attack_type]}: {proof[0].detail}", proof[0].source))
    c5 = _rows(evidence, "C5", "verified")
    if c5:
        ruled.append(RuledOut("wrong_geography", f"wrong geography: {c5[0].detail}", c5[0].source))

    grade = {p.attack_type: p.block_grade for p in PATTERNS}
    matches.sort(key=lambda m: (not grade[m.attack_type], -len(m.supporting), INDEX[m.attack_type]))
    names = [m.attack_type for m in matches]
    hyps = [Hypothesis(m.attack_type, m.title, _narrative(m.attack_type, evidence, inv, names[1:]),
                       sorted(dict.fromkeys(e.check for e in m.required + m.supporting)))
            for m in matches]
    if not hyps:
        if any(e.status == "contradicted" for e in evidence):
            text = "Some checks raised concerns, but they do not match a known attack pattern."
        elif low_confidence:
            text = ("No contradiction found, but critical facts could not be verified; "
                    "this is missing data, not evidence of fraud.")
        else:
            text = "No attack pattern detected: identity, channel and payment history all check out."
        return Hypothesis("none", "No attack pattern detected", text), [], ruled
    return hyps[0], hyps[1:], ruled
