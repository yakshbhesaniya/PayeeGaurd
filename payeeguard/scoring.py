"""Decision engine (spec 10): risk from family maxima, confidence from three critical facts, rules R1-R7.

Pure functions over Evidence: no I/O, no LLM. The same evidence always gives the same decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .config import policy, to_usd
from .models import Evidence, Invoice

Decision = Literal["PAY", "HOLD", "BLOCK"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
MAX_FAMILIES = ("CHANNEL", "SIGNATORY", "COMPANY", "PAYMENT", "INTEGRITY")
BEHAVIOUR_CAP = 25


@dataclass
class Score:
    decision: Decision
    risk: int
    confidence: Confidence
    rule: str
    facts: list[str]
    notes: list[str] = field(default_factory=list)
    kyb_task: bool = False


def is_check(e: Evidence, prefix: str) -> bool:
    """'G1' matches 'G1_account_history' but not 'G10_x'."""
    return e.check == prefix or e.check.startswith(prefix + "_")


def has(evidence: list[Evidence], prefix: str, status: str) -> bool:
    return any(is_check(e, prefix) and e.status == status for e in evidence)


def family_risk(evidence: list[Evidence]) -> dict[str, int]:
    """One fact counts once: each family contributes its maximum; BEHAVIOUR sums, capped at 25."""
    out: dict[str, int] = {}
    for e in evidence:
        if e.status != "contradicted" or e.family == "CONTEXT":
            continue
        w = max(0, int(e.weight))
        if e.family == "BEHAVIOUR":
            out["BEHAVIOUR"] = min(BEHAVIOUR_CAP, out.get("BEHAVIOUR", 0) + w)
        else:
            out[e.family] = max(out.get(e.family, 0), w)
    return out


def risk_score(evidence: list[Evidence]) -> int:
    return min(100, sum(family_risk(evidence).values()))


def confirmed_facts(evidence: list[Evidence], ctx: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    verified = [e for e in evidence if e.status == "verified"]
    if any(e.confirms == "who" for e in verified) or ctx.get("vendor_known"):
        facts.append("who")
    if any(e.confirms == "channel" for e in verified):
        facts.append("channel")
    money = [e for e in verified if e.confirms == "money"]
    geo_only = money and all(is_check(e, "C5") for e in money)
    if money and not (geo_only and has(evidence, "C11", "contradicted")):
        facts.append("money")
    return facts


def confidence_level(n: int) -> Confidence:
    return "HIGH" if n >= 3 else "MEDIUM" if n == 2 else "LOW"


def _amount_problem(inv: Invoice, ctx: dict[str, Any]) -> Optional[str]:
    if inv.amount is None:
        return "amount missing"
    if inv.amount <= 0:
        return "amount is zero or negative"
    if ctx.get("credit_note"):
        return "credit note"
    return None


def _first(evidence: list[Evidence], pred) -> Optional[Evidence]:
    return next((e for e in evidence if pred(e)), None)


def decide(inv: Invoice, evidence: list[Evidence], ctx: Optional[dict[str, Any]] = None) -> Score:
    ctx = ctx or {}
    pol = policy()["decision"]
    risk = risk_score(evidence)
    facts = confirmed_facts(evidence, ctx)
    conf = confidence_level(len(facts))
    notes: list[str] = []
    kyb = has(evidence, "C1", "unchecked") and any(
        is_check(e, "C1") and e.unchecked_reason == "no_data" for e in evidence)

    hard = _first(evidence, lambda e: e.hard_block and e.status == "contradicted")
    if hard:
        return Score("BLOCK", risk, conf, f"R1 hard fact {hard.check}", facts, notes, kyb)
    if risk >= int(pol["block_risk"]):
        return Score("BLOCK", risk, conf, f"R2 risk {risk} >= {pol['block_risk']}", facts, notes, kyb)

    r3: list[str] = []
    if inv.bank_change_requested:
        r3.append("bank change requested")
    if has(evidence, "G1", "contradicted"):
        r3.append("account never paid to this vendor")
    if has(evidence, "G5", "contradicted"):
        r3.append("two different accounts given")
    problem = _amount_problem(inv, ctx)
    if problem:
        r3.append(problem)
    if r3:
        return Score("HOLD", risk, conf, "R3 " + "; ".join(r3), facts, notes, kyb)
    if risk >= int(pol["hold_risk"]):
        return Score("HOLD", risk, conf, f"R4 risk {risk} >= {pol['hold_risk']}", facts, notes, kyb)
    if conf == "HIGH":
        return Score("PAY", risk, conf, "R5 confidence HIGH", facts, notes, kyb)
    usd = to_usd(inv.amount, inv.currency)
    limit = float(pol["auto_pay_limit_usd"])
    if conf == "MEDIUM" and usd is not None and usd <= limit:
        return Score("PAY", risk, conf, f"R6 confidence MEDIUM, USD {usd:,.2f} <= limit {limit:,.0f}", facts, notes, kyb)

    if conf == "MEDIUM":
        why = "amount above auto-pay limit" if usd is not None else "currency unknown, treated as above the limit"
    else:
        why = "confidence LOW"
    if any(e.unchecked_reason == "api_error" for e in evidence) and conf == "LOW":
        notes.append("could not verify: do not pay until checks run")
    return Score("HOLD", risk, conf, f"R7 {why}", facts, notes, kyb)


# ---------- Mode 2: vendor onboarding (spec 3.1) ----------

def decide_onboarding(evidence: list[Evidence]) -> tuple[str, str]:
    """REJECT on a hard fact (lookalike / unregistered website); REVIEW on any doubt; else APPROVE."""
    hard = _first(evidence, lambda e: e.status == "contradicted" and (
        e.hard_block or "not registered" in e.detail.lower()))
    if hard:
        return "REJECT", f"hard fact {hard.check}: {hard.detail}"
    bad = _first(evidence, lambda e: e.status == "contradicted")
    if bad:
        return "REVIEW", f"{bad.check}: {bad.detail}"
    if not any(e.status == "verified" and e.confirms == "who" for e in evidence):
        return "REVIEW", "company could not be verified in Crustdata"
    if any(e.unchecked_reason == "api_error" for e in evidence):
        return "REVIEW", "some checks could not run"
    return "APPROVE", "company verified, no contradictions"
