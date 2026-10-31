"""Decision engine (spec section 10): risk = sum of per-family maxima; confidence = 3 critical facts.

Only positive contradictions add risk. Missing data lowers confidence, never adds risk.
"""

from dataclasses import dataclass

from . import config
from .models import Evidence

MAX_FAMILIES = ("CHANNEL", "SIGNATORY", "COMPANY", "PAYMENT", "INTEGRITY")
BEHAVIOUR_CAP = 25


@dataclass
class Score:
    risk: int
    confidence: str
    decision: str
    rule: str
    facts: dict
    families: dict


def family_risk(evidence: list[Evidence]) -> dict[str, int]:
    fam: dict[str, int] = {}
    for e in evidence:
        if e.status != "contradicted" or e.family == "CONTEXT":
            continue
        if e.family == "BEHAVIOUR":
            fam["BEHAVIOUR"] = min(BEHAVIOUR_CAP, fam.get("BEHAVIOUR", 0) + e.weight)
        else:
            fam[e.family] = max(fam.get(e.family, 0), e.weight)
    return fam


def confidence_of(evidence: list[Evidence], vendor_known: bool) -> tuple[str, dict]:
    bad_bank = any(e.check == "C11_bank_format" and e.status == "contradicted" for e in evidence)
    # Geography alone can't confirm MONEY for a first-time vendor if the bank details themselves are malformed (10.3).
    confirmed = {e.confirms for e in evidence if e.status == "verified" and e.confirms
                 and not (e.check == "C5_geo" and bad_bank)}
    facts = {"who": "who" in confirmed or vendor_known, "channel": "channel" in confirmed, "money": "money" in confirmed}
    n = sum(facts.values())
    return ("HIGH" if n == 3 else "MEDIUM" if n == 2 else "LOW"), facts


def to_usd(amount: float | None, currency: str | None) -> float | None:
    if amount is None:
        return None
    rate = config.policy()["fx_to_usd"].get((currency or "").upper())
    return amount * rate if rate else None


def score(evidence: list[Evidence], *, vendor_known: bool, amount: float | None, currency: str | None,
          bank_change: bool, credit_note: bool = False) -> Score:
    pol = config.policy()["decision"]
    fam = family_risk(evidence)
    risk = min(100, sum(fam.values()))
    conf, facts = confidence_of(evidence, vendor_known)

    def done(decision, rule):
        return Score(risk, conf, decision, rule, facts, fam)

    hard = [e for e in evidence if e.hard_block and e.status == "contradicted"]
    if hard:
        return done("BLOCK", f"R1 hard fact {hard[0].check}")
    if risk >= pol["block_risk"]:
        return done("BLOCK", f"R2 risk {risk} >= {pol['block_risk']}")
    status = {e.check: e for e in evidence}
    g1 = status.get("G1_account_history")
    if bank_change or (g1 and g1.status == "contradicted") or "G5_ambiguous_payment" in status:
        return done("HOLD", "R3 payment-instruction change: never auto-paid")
    if amount is None or amount <= 0 or credit_note:
        return done("HOLD", "R3 amount missing, zero or a credit note")
    c1 = status.get("C1_vendor")
    if c1 and c1.status == "unchecked" and c1.unchecked_reason in ("api_error", "not_enabled"):
        return done("HOLD", "R3 Crustdata unavailable: could not verify the vendor, do not pay until checks run")
    if risk >= pol["hold_risk"]:
        return done("HOLD", f"R4 risk {risk} >= {pol['hold_risk']}")
    if conf == "HIGH":
        return done("PAY", "R5 low risk, HIGH confidence")
    usd = to_usd(amount, currency)
    if conf == "MEDIUM" and usd is not None and usd <= pol["auto_pay_limit_usd"]:
        return done("PAY", f"R6 low risk, MEDIUM confidence, USD {usd:,.0f} <= auto-pay limit")
    if conf == "MEDIUM":
        return done("HOLD", "R7 MEDIUM confidence above the auto-pay limit (or unknown currency)")
    return done("HOLD", "R7 LOW confidence: not enough verified facts (missing data is not fraud)")
