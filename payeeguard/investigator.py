"""Fraud Pattern Investigator (spec section 9): names the most likely attack and what was ruled out.

Deterministic pattern library over the evidence. Narratives are built only from cited evidence details.
"""

from dataclasses import dataclass
from typing import Callable

from .models import Evidence, Hypothesis, RuledOut


class E:
    """Evidence lookup helpers."""

    def __init__(self, evidence: list[Evidence]):
        self.rows = evidence

    def c(self, *checks) -> bool:
        return any(e.check in checks and e.status == "contradicted" for e in self.rows)

    def v(self, *checks) -> bool:
        return any(e.check in checks and e.status == "verified" for e in self.rows)

    def u(self, check, reasons=("no_data",)) -> bool:
        return any(e.check == check and e.status == "unchecked" and e.unchecked_reason in reasons for e in self.rows)

    def get(self, *checks) -> list[Evidence]:
        return [e for e in self.rows if e.check in checks and e.status != "unchecked"]


@dataclass
class Pattern:
    attack_type: str
    title: str
    lead: str
    required: Callable[[E], bool]
    supporting: tuple[str, ...]
    ruled_out_by: Callable[[E], list[Evidence]] | None = None


def _verified(*checks):
    return lambda x: [e for e in x.get(*checks) if e.status == "verified"]


PATTERNS = [
    Pattern("mule_campaign", "Account linked to a prior blocked attack",
            "The payment account was already used in a blocked fraud attempt.",
            lambda x: x.c("G3_account_flagged"), ("G3_account_flagged", "G1_account_history", "G2_account_other_vendor"),
            _verified("G1_account_history")),
    Pattern("vendor_impersonation", "Lookalike-domain impersonation",
            "The sender imitates the vendor's domain.",
            lambda x: x.c("C2_lookalike", "C10_reply_to_lookalike"),
            ("C2_lookalike", "C10_reply_to_lookalike", "C8_domain_age", "C5_geo", "C14_urgency", "C14_payment_method"),
            lambda x: [e for e in x.get("C2_domain") if e.status == "verified"] if not x.c("C10_reply_to",
                                                                                           "C10_reply_to_lookalike") else []),
    Pattern("duplicate_replay", "Already-paid invoice re-sent",
            "This invoice number has already been paid.",
            lambda x: x.c("G4_duplicate"), ("G4_duplicate", "G1_account_history")),
    Pattern("terminated_vendor", "Invoice from a vendor we ended the relationship with",
            "Our own Slack or email history shows we stopped working with this vendor, and nobody re-engaged since.",
            lambda x: x.c("C15_relationship"), ("C15_relationship", "G1_account_history", "C14_urgency")),
    Pattern("compromised_vendor_mailbox", "Real vendor mailbox, new payment details",
            "The email comes from the vendor's real domain but the payment details don't match its history.",
            lambda x: x.v("C2_domain") and x.c("G1_account_history", "G3_account_flagged", "C10_reply_to"),
            ("C2_domain", "G1_account_history", "G3_account_flagged", "C10_reply_to", "C5_geo"),
            _verified("G1_account_history")),
    Pattern("ex_employee", "Former employee requesting payment",
            "The sender used to work at the vendor but no longer does.",
            lambda x: x.c("C3_ex_employee"), ("C3_ex_employee", "C2b_freemail", "G1_account_history"),
            _verified("C3_signatory")),
    Pattern("unauthorized_change", "Real employee outside the payments role",
            "A genuine employee who doesn't work in finance is asking to change payment details.",
            lambda x: x.c("C3_role_authority"), ("C3_role_authority", "G1_account_history")),
    Pattern("unverified_signatory", "Unverified person on a personal mailbox",
            "The sender uses a personal mailbox and can't be verified as working at the vendor.",
            lambda x: x.c("C2b_freemail") and not x.v("C3_signatory"),
            ("C2b_freemail", "C14_channel_switch", "C3_works_elsewhere"), _verified("C3_signatory")),
    Pattern("payment_method_fraud", "Off-rails payment request",
            "The request asks for crypto, gift cards or a personal account.",
            lambda x: x.c("C14_payment_method"), ("C14_payment_method",)),
    Pattern("unverifiable_vendor", "Vendor we can't verify (not proof of fraud)",
            "The vendor isn't in Crustdata and has no history with us, so a human must verify it.",
            lambda x: x.u("C1_vendor"), ("C1_vendor", "C8_domain_age", "C4_shell", "C2b_freemail"),
            _verified("C1_vendor")),
]

EXTRA_RULED_OUT = [
    ("fake_vendor", "C1_vendor", "the vendor is a real company"),
    ("wrong_geography", "C5_geo", "the bank is in a country where the vendor operates"),
]


def investigate(evidence: list[Evidence]) -> tuple[Hypothesis | None, list[Hypothesis], list[RuledOut]]:
    x = E(evidence)
    matched: list[Hypothesis] = []
    ruled: list[RuledOut] = []
    for p in PATTERNS:
        if p.required(x):
            rows = [e for e in x.get(*p.supporting) if e.status == "contradicted"]
            narrative = p.lead + (" " + " ".join(f"{e.detail} [{e.source}]." for e in rows[:4]) if rows else "")
            matched.append(Hypothesis(p.attack_type, p.title, narrative, [e.check for e in rows]))
        elif p.ruled_out_by:
            for e in p.ruled_out_by(x)[:1]:
                ruled.append(RuledOut(p.attack_type, e.detail, e.source))
    seen = {r.attack_type for r in ruled}
    for attack, check, text in EXTRA_RULED_OUT:
        for e in x.get(check):
            if e.status == "verified" and attack not in seen:
                ruled.append(RuledOut(attack, f"{text}: {e.detail}", e.source))
    if not matched:
        return None, [], ruled
    return matched[0], matched[1:], ruled
