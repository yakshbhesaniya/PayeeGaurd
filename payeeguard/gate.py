"""Payment gate + human-in-the-loop state machine (spec 11, 13.6).

States: NEW -> PAY | HOLD | BLOCK;  HOLD -> APPROVED | REJECTED (=BLOCK);  PAY | APPROVED -> PAID_SIMULATED.
Case records hold no raw email text and no full account numbers (Y1, Y2).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import wiring
from .config import data_dir, payments_dir, policy, to_usd
from .extract import mask_account
from .models import Evidence, Hypothesis, Invoice, RuledOut, Verdict
from .pipeline import _graph_call, sender_domain

HUMAN_STATES = {"APPROVED", "REJECTED", "PAID_SIMULATED"}


def _cases() -> Path:
    d = data_dir() / "cases"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, text: str) -> None:
    """M6: write a temp file, then rename over the target."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_case(case_id: str) -> Optional[dict[str, Any]]:
    if not case_id or not all(c.isalnum() or c == "-" for c in case_id):
        return None
    path = _cases() / f"{case_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _store(rec: dict[str, Any]) -> None:
    _atomic_write(_cases() / f"{rec['case_id']}.json", json.dumps(rec, indent=2, ensure_ascii=False, default=str))


def _ev_dict(e: Evidence) -> dict[str, Any]:
    return dict(e.__dict__)


def save_case(verdict: Verdict, inv: Invoice, ctx: dict[str, Any]) -> dict[str, Any]:
    comp = ctx.get("company") if isinstance(ctx.get("company"), dict) else {}
    old = load_case(verdict.case_id) or {}
    rec = {
        "case_id": verdict.case_id, "decision": verdict.decision, "decision_rule": verdict.decision_rule,
        "risk": verdict.risk_score, "confidence": verdict.confidence, "summary": verdict.summary,
        "attack_type": verdict.hypothesis.attack_type if verdict.hypothesis else "none",
        "title": verdict.hypothesis.title if verdict.hypothesis else "",
        "narrative": verdict.hypothesis.narrative if verdict.hypothesis else "",
        "also_matched": [h.title for h in verdict.also_matched],
        "ruled_out": [r.__dict__ for r in verdict.ruled_out],
        "evidence": [_ev_dict(e) for e in verdict.evidence],
        "company_id": verdict.company_id, "person_id": verdict.person_id, "callback": verdict.callback,
        "vendor_name": inv.vendor_name, "vendor_domain": inv.vendor_domain, "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date, "amount": inv.amount, "currency": inv.currency,
        "sender_email": inv.sender_email, "reply_to_email": inv.reply_to_email, "bank_swift": inv.bank_swift,
        "remit_country": inv.remit_country, "last4": inv.bank_account_last4, "account": mask_account(inv),
        "has_account": bool(inv.bank_iban or inv.bank_account), "bank_change_requested": inv.bank_change_requested,
        "account_node": ctx.get("account_node") or ctx.get("ledger_account"),
        "related_cases": ctx.get("related_cases") or [], "display_name": ctx.get("display_name"),
        "company": {"basic_info": (comp or {}).get("basic_info") or {},
                    "locations": {"all_office_addresses": ((comp or {}).get("locations") or {})
                                  .get("all_office_addresses") or []}},
        "created_at": old.get("created_at") or wiring.now(),
        "status": verdict.decision, "approvals": [], "history": [],
    }
    overridden = any(a.get("override") for a in old.get("approvals", []))
    if old.get("status") == "APPROVED" and verdict.decision == "BLOCK" and not overridden:
        # new hard evidence (e.g. the account was since flagged in another case) voids an earlier approval
        rec["history"] = old.get("history", []) + [{"event": "approval_voided", "at": wiring.now(),
                                                    "rule": verdict.decision_rule}]
        wiring.audit_append({"event": "approval_voided", "case_id": verdict.case_id, "rule": verdict.decision_rule})
    elif old.get("status") in HUMAN_STATES or old.get("approvals"):   # G-3: a re-run never undoes the gate
        rec.update(status=old["status"], decision=old.get("decision", rec["decision"]),
                   approvals=old.get("approvals", []), history=old.get("history", []))
    _store(rec)
    return rec


def _invoice(rec: dict[str, Any]) -> Invoice:
    return Invoice(rec.get("vendor_name"), rec.get("vendor_domain"), rec.get("sender_email"), None, None,
                   rec.get("remit_country"), None, rec.get("amount"), rec.get("currency"),
                   bank_change_requested=bool(rec.get("bank_change_requested")), invoice_number=rec.get("invoice_number"),
                   invoice_date=rec.get("invoice_date"), reply_to_email=rec.get("reply_to_email"),
                   bank_swift=rec.get("bank_swift"), bank_account_last4=rec.get("last4"))


def _verdict(rec: dict[str, Any], decision: str) -> Verdict:
    ev = [Evidence(**e) for e in rec.get("evidence", [])]
    hyp = Hypothesis(rec.get("attack_type", "none"), rec.get("title", ""), rec.get("narrative", ""))
    return Verdict(decision, rec.get("risk", 0), rec.get("confidence", "LOW"), evidence=ev, hypothesis=hyp,  # type: ignore[arg-type]
                   ruled_out=[RuledOut(**r) for r in rec.get("ruled_out", [])],
                   decision_rule=rec.get("decision_rule", ""), company_id=rec.get("company_id"),
                   person_id=rec.get("person_id"), callback=rec.get("callback"), summary=rec.get("summary", ""),
                   case_id=rec["case_id"])


def _top_evidence(rec: dict[str, Any], n: int = 3) -> str:
    rows = sorted((e for e in rec.get("evidence", []) if e["status"] == "contradicted"),
                  key=lambda e: (not e["hard_block"], -e["weight"]))[:n]
    if not rows:
        rows = [e for e in rec.get("evidence", []) if e["status"] == "unchecked"][:n]
    return "; ".join(f"{e['check']}: {e['detail']}" for e in rows) or "no evidence rows"


def _usd(rec: dict[str, Any]) -> Optional[float]:
    return to_usd(rec.get("amount"), rec.get("currency"))


def needs_dual(rec: dict[str, Any]) -> bool:
    limit = policy()["decision"].get("dual_approval_above_usd")
    if not limit:
        return False
    usd = _usd(rec)
    return usd is None or usd > float(limit)


def _approvers(rec: dict[str, Any]) -> list[str]:
    return [a["reviewer"] for a in rec.get("approvals", []) if a.get("decision") == "approve"]


def _event(rec: dict[str, Any], kind: str, **kw: Any) -> None:
    ev = {"event": kind, "case_id": rec["case_id"], "at": wiring.now(), **kw}
    rec.setdefault("history", []).append(ev)
    wiring.audit_append(ev)


# ---------- tools ----------

def pay_invoice(case_id: str, graph: Any = None) -> str:
    rec = load_case(case_id)
    if rec is None:
        return f"Error: unknown case_id {case_id!r}. Run verify_payee first."             # P1: no write
    status = rec["status"]
    if status == "PAID_SIMULATED":
        return f"Refused: case {case_id} is already PAID_SIMULATED; a second payment is never made."  # G-3
    if status not in ("PAY", "APPROVED"):
        why = "awaiting human approval (resolve_case)" if status == "HOLD" else "blocked"
        return (f"Refused: case {case_id} is {status} ({why}). Rule {rec['decision_rule']}. "
                f"Top evidence: {_top_evidence(rec)}")
    if needs_dual(rec) and len(set(_approvers(rec))) < 2:
        return (f"Refused: {rec.get('currency')} {rec.get('amount')} is above the dual-approval limit; two different "
                f"reviewers must approve via resolve_case ({len(set(_approvers(rec)))} of 2 so far).")
    line = {"simulated": True, "case_id": case_id, "vendor": rec.get("vendor_name"), "company_id": rec.get("company_id"),
            "invoice_number": rec.get("invoice_number"), "amount": rec.get("amount"), "currency": rec.get("currency"),
            "account": rec.get("account") if rec.get("has_account") else "ledger account on file", "at": wiring.now()}
    out = payments_dir()
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "outbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    graph = graph if graph is not None else wiring.open_graph()
    cite = f"gate:pay_invoice@case:{case_id}"
    if rec.get("company_id") and rec.get("account_node"):
        _graph_call(graph, "add_edge", f"company:{rec['company_id']}", "PAID", rec["account_node"], cite, case_id)
    dom = sender_domain(_invoice(rec))
    if rec.get("company_id") and dom:
        _graph_call(graph, "add_edge", f"company:{rec['company_id']}", "PAID_FROM_DOMAIN", f"domain:{dom}", cite,
                    case_id)
    rec["status"] = "PAID_SIMULATED"
    _event(rec, "paid_simulated", amount=rec.get("amount"), currency=rec.get("currency"))
    _store(rec)
    return (f"SIMULATED payment queued for case {case_id}: {rec.get('currency')} {rec.get('amount')} to "
            f"{line['account']} (payments/outbox.jsonl). No money moved.")


def resolve_case(case_id: str, decision: str, reviewer: str, note: str = "", graph: Any = None) -> str:
    decision = (decision or "").strip().lower()
    reviewer = (reviewer or "").strip()
    if decision not in ("approve", "reject", "override"):
        return "Error: decision must be approve, reject or override."
    if not reviewer:
        return "Error: reviewer is required."
    rec = load_case(case_id)
    if rec is None:
        return f"Error: unknown case_id {case_id!r}."
    status = rec["status"]
    if status == "PAID_SIMULATED":
        return f"Case {case_id} is already PAID_SIMULATED; nothing to resolve."
    graph = graph if graph is not None else wiring.open_graph()

    if status in ("BLOCK", "REJECTED"):
        if decision != "override":
            return f"Case {case_id} is {status}. Only 'override' (with a note) can reopen a block."
        if not note.strip():
            return "Error: override requires a note explaining why the block was wrong."
        revoked = _graph_call(graph, "revoke_case_flags", case_id, wiring.now(), default=0) or 0
        rec["status"] = "APPROVED"
        rec["approvals"] = rec.get("approvals", []) + [{"reviewer": reviewer, "decision": "approve", "note": note,
                                                        "at": wiring.now(), "override": True}]
        _event(rec, "override", reviewer=reviewer, note=note, revoked_flags=revoked)
        _store(rec)
        return f"Override recorded by {reviewer}: case {case_id} moved to APPROVED; {revoked} FLAGGED_IN edge(s) revoked."

    if decision == "override":
        return f"Case {case_id} is {status}; use approve or reject (override is only for blocks)."
    if decision == "approve" and status == "PAY" and not needs_dual(rec):
        return f"Case {case_id} is already payable (PAY); no approval needed."                 # P3

    if decision == "reject":
        rec["status"] = "REJECTED"
        rec["decision"] = "BLOCK"
        _event(rec, "rejected", reviewer=reviewer, note=note)
        _store(rec)
        from .actions import write_rejected_case
        ctx = {"related_cases": rec.get("related_cases"), "company": rec.get("company"),
               "display_name": rec.get("display_name"), "account_node": rec.get("account_node")}
        write_rejected_case(_verdict(rec, "BLOCK"), _invoice(rec), ctx)
        if rec.get("account_node") and not str(rec["account_node"]).startswith("account:weak:") and rec.get("has_account"):
            _graph_call(graph, "add_edge", rec["account_node"], "FLAGGED_IN", f"case:{case_id}",
                        f"gate:reject@case:{case_id}", case_id)
        return f"Case {case_id} rejected by {reviewer}: now BLOCK; case file written and the account flagged."

    if reviewer in _approvers(rec):
        return f"Refused: {reviewer} already approved case {case_id}; a second approval needs a different reviewer."  # P5
    rec["approvals"] = rec.get("approvals", []) + [{"reviewer": reviewer, "decision": "approve", "note": note,
                                                    "at": wiring.now()}]
    n = len(set(_approvers(rec)))
    if needs_dual(rec) and n < 2:
        _event(rec, "approval", reviewer=reviewer, note=note, approvals=n)
        _store(rec)
        return f"Approval 1 of 2 recorded for case {case_id}; a different reviewer must also approve."
    if status == "HOLD":
        rec["status"] = "APPROVED"
    _event(rec, "approved", reviewer=reviewer, note=note, approvals=n)
    _store(rec)
    return f"Case {case_id} approved by {reviewer}; pay_invoice(\"{case_id}\") may now proceed (SIMULATED)."


def case_status(case_id: str) -> str:
    rec = load_case(case_id)
    if rec is None:
        return f"Error: unknown case_id {case_id!r}."
    approvers = ", ".join(_approvers(rec)) or "none"
    return (f"Case {case_id}: status {rec['status']} · decision {rec['decision']} · rule {rec['decision_rule']} · "
            f"risk {rec['risk']} · confidence {rec['confidence']} · {rec.get('title') or 'no attack pattern'} · "
            f"approvals: {approvers}")
