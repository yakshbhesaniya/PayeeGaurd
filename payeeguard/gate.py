"""Payment gate and human-in-the-loop (spec section 11). The only way money (simulated) moves."""

import json
from datetime import date

from . import audit, config
from .checks_company import create_watch
from .graph import Fingerprint, Graph
from .scoring import to_usd

PAYABLE = ("PAY", "APPROVED")


def _outbox(entry: dict) -> None:
    p = config.ROOT / "payments" / "outbox.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _fp(d: dict) -> Fingerprint | None:
    f = d.get("fp")
    return Fingerprint(**f) if f else None


def pay_invoice(case_id: str, graph: Graph, client=None) -> str:
    case = graph.get_case(case_id)
    if not case:
        return f"Unknown case `{case_id}`. Run verify_payee first."           # edge case P1
    d = case["data"]
    if case["state"] == "PAID_SIMULATED":
        return f"Refused: case `{case_id}` was already paid (simulated). No double payment."   # G-3
    if case["state"] not in PAYABLE:
        why = d.get("top_reason") or d.get("rule")
        audit.append("pay_refused", case_id=case_id, state=case["state"], reason=why)
        return (f"Refused: case `{case_id}` is {case['state']} ({d.get('rule')}). Top evidence: {why}. "
                "A HOLD needs `resolve_case(..., 'approve', ...)` first; a BLOCK cannot be paid.")
    limit = config.policy()["decision"]["dual_approval_above_usd"]
    usd = to_usd(d.get("amount"), d.get("currency"))
    approvers = {a["reviewer"] for a in d.get("approvals", []) if a["decision"] in ("approve", "override")}
    if usd is not None and usd > limit and len(approvers) < 2:
        return (f"Refused: USD {usd:,.0f} is above the dual-approval limit ({limit:,}); "
                f"{len(approvers)} of 2 different reviewers have approved.")
    fp = _fp(d)
    graph.record_payment(case_id, d["company_node"], d.get("invoice_number") or case_id, d.get("amount"),
                         d.get("currency"), fp, date.today().isoformat(), f"gate:pay_invoice@case:{case_id}",
                         d.get("sender_domain"))
    graph.set_state(case_id, "PAID_SIMULATED")
    _outbox({"case_id": case_id, "vendor": d.get("vendor"), "amount": d.get("amount"), "currency": d.get("currency"),
             "account": fp.masked if fp else "account on file", "simulated": True})
    h = audit.append("paid_simulated", case_id=case_id, amount=d.get("amount"), currency=d.get("currency"))
    watch = ""
    if client is not None and d.get("company_id") and not config.offline():
        watch = "\n" + create_watch(client, d["company_id"])
    return (f"SIMULATED payment queued: {d.get('currency')} {d.get('amount'):,.2f} to {d.get('vendor')} "
            f"({fp.masked if fp else 'account on file'}). Vendor history updated. Audit {h[:12]}.{watch}")


def resolve_case(case_id: str, decision: str, reviewer: str, note: str, graph: Graph) -> str:
    case = graph.get_case(case_id)
    if not case:
        return f"Unknown case `{case_id}`."
    if decision not in ("approve", "reject", "override"):
        return "decision must be approve, reject or override"
    if not reviewer or not note:
        return "reviewer and note are required (they go into the audit log)"
    d, state = case["data"], case["state"]
    if state in ("PAY", "PAID_SIMULATED"):
        return f"Case `{case_id}` is {state}: nothing to resolve."             # P3
    if decision == "override" and state != "BLOCK":
        return "override is only for a BLOCK that was wrong; use approve/reject for a HOLD"
    if decision in ("approve", "reject") and state not in ("HOLD", "APPROVED"):
        return f"Case `{case_id}` is {state}; only a HOLD can be approved or rejected (use override for a wrong BLOCK)."
    approvals = d.setdefault("approvals", [])
    if any(a["reviewer"] == reviewer and a["decision"] == decision for a in approvals):
        return f"{reviewer} already recorded '{decision}' on this case; a second approval needs a different reviewer."
    approvals.append({"reviewer": reviewer, "decision": decision, "note": note})
    if decision == "reject":
        nodes = [n for n in ((d.get("fp") or {}).get("id"), d.get("sender_node")) if n]
        graph.flag(case_id, nodes, f"human:resolve_case reject by {reviewer}")
        graph.set_state(case_id, "BLOCK", d)
        audit.append("resolved", case_id=case_id, decision=decision, reviewer=reviewer, note=note)
        return f"Case `{case_id}` rejected by {reviewer}: now BLOCK, indicators flagged for future checks."
    if decision == "override":
        graph.revoke(case_id)                                                # G-7
    graph.set_state(case_id, "APPROVED", d)
    audit.append("resolved", case_id=case_id, decision=decision, reviewer=reviewer, note=note)
    return f"Case `{case_id}` {decision}d by {reviewer}. It can now be paid with `pay_invoice('{case_id}')`."


def case_status(case_id: str, graph: Graph) -> str:
    case = graph.get_case(case_id)
    if not case:
        return f"Unknown case `{case_id}`."
    d = case["data"]
    appr = ", ".join(f"{a['decision']} by {a['reviewer']}" for a in d.get("approvals", [])) or "none"
    return (f"Case `{case_id}`: decision {case['decision']}, state {case['state']}, vendor {d.get('vendor')}, "
            f"amount {d.get('currency')} {d.get('amount')}, rule {d.get('rule')}, approvals: {appr}.")
