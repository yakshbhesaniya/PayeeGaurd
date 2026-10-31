"""Ablation: which evidence decides each verdict? Re-decide each sample with one check family removed at a time.

Re-scoring is exact (scoring + investigator are pure functions of the evidence), so no extra API calls.
"""

from .investigator import investigate
from .scoring import score


def ablate(verdict, inv, vendor_known: bool, credit_note: bool = False) -> list[tuple[str, str, str]]:
    """Return (check removed, decision without it, attack without it) for every check that fired or verified."""
    rows = []
    checks = sorted({e.check for e in verdict.evidence if e.status != "unchecked"})
    for chk in checks:
        rest = [e for e in verdict.evidence if e.check != chk]
        s = score(rest, vendor_known=vendor_known, amount=inv.amount, currency=inv.currency,
                  bank_change=inv.bank_change_requested, credit_note=credit_note)
        hyp, _, _ = investigate(rest)
        rows.append((chk, s.decision, hyp.attack_type if hyp else "none"))
    return rows
