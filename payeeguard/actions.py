"""Finished work (spec 11): every artifact is written to reports/<case_id>/ and labelled SIMULATED / DRAFT."""
from __future__ import annotations

import json
from typing import Any, Optional

from . import wiring
from .extract import mask_account
from .models import Action, Evidence, Invoice, Verdict
from .pipeline import evidence_hash, sender_domain
from .report import case_dir, ledger_table, redact, stamp

VERSION = "payeeguard 0.3"
BANNER = "> **SIMULATED / DRAFT: not sent.** Nothing here contacts anyone or moves money.\n"


def _write(case_id: str, name: str, body: str, inv: Optional[Invoice] = None) -> str:
    path = case_dir(case_id) / name
    path.write_text(redact(body, inv), encoding="utf-8")
    return str(path)


def _contact(verdict: Verdict, ctx: dict[str, Any]) -> dict[str, Optional[str]]:
    cb = ctx.get("callback")
    if isinstance(cb, dict):
        return {"phone": cb.get("phone"), "email": cb.get("email"), "source": cb.get("source") or cb.get("url")}
    return {"phone": verdict.callback, "email": None, "source": ctx.get("callback_source")}


def _top(verdict: Verdict, n: int = 5) -> list[Evidence]:
    from .report import ledger
    return [e for e in ledger(verdict.evidence) if e.status != "verified"][:n]


def _bullets(rows: list[Evidence]) -> str:
    return "\n".join(f"- **{e.check}** ({e.status}{' +' + str(e.weight) if e.weight else ''}): {e.detail} "
                     f"[{e.source}]" for e in rows) or "- none"


def _amount(inv: Invoice) -> str:
    return f"{inv.currency or '???'} {inv.amount:,.2f}" if inv.amount is not None else "amount unknown"


def _has_no_data_c1(verdict: Verdict) -> bool:
    return any(e.check.startswith("C1") and e.status == "unchecked" and e.unchecked_reason == "no_data"
               for e in verdict.evidence)


# ---------- PAY ----------

def _payment_instruction(verdict: Verdict, inv: Invoice, ctx: dict[str, Any]) -> Action:
    body = {"simulated": True, "case_id": verdict.case_id, "vendor": inv.vendor_name, "company_id": verdict.company_id,
            "amount": inv.amount, "currency": inv.currency, "invoice_number": inv.invoice_number,
            "account": mask_account(inv) if (inv.bank_iban or inv.bank_account) else "ledger account on file",
            "account_fingerprint": ctx.get("account_node") or ctx.get("ledger_account"),
            "risk": verdict.risk_score, "confidence": verdict.confidence, "decision_rule": verdict.decision_rule,
            "evidence_hash": evidence_hash(verdict.evidence)}
    text = json.dumps(body, indent=2, ensure_ascii=False)
    return Action("payment_instruction", "Payment instruction (awaiting gate)", text,
                  _write(verdict.case_id, "payment_instruction.json", text, inv))


# ---------- HOLD ----------

def _challenges(inv: Invoice, ctx: dict[str, Any]) -> list[str]:
    qs = []
    for no in (ctx.get("paid_invoices") or [])[-2:]:
        qs.append(f"What was the amount of invoice {no}?")
    qs += ["What are the last 4 digits of the account we have paid you before?",
           "Who in your finance team approved this change, and from which official address?"]
    if inv.bank_change_requested or inv.bank_iban or inv.bank_account:
        qs.append(f"Please confirm the account ending {inv.bank_account_last4 or '????'} belongs to "
                  f"{inv.vendor_name or 'your company'} (bank name and country).")
    return qs


def _hold_actions(verdict: Verdict, inv: Invoice, ctx: dict[str, Any]) -> list[Action]:
    c = _contact(verdict, ctx)
    vendor = inv.vendor_name or "the vendor"
    where = c["phone"] or "the official number (look it up independently: never use the number in the email)"
    src = f" (source: {c['source']})" if c["source"] else ""
    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(_challenges(inv, ctx), 1))
    script = (f"{BANNER}\n# Callback script: case {verdict.case_id}\n\n"
              f"Call **{vendor}** on **{where}**{src}.\nDo **not** use any phone number or email from the invoice.\n\n"
              f"Say: \"We received a payment request for invoice {inv.invoice_number or '(no number)'} ({_amount(inv)}). "
              f"Before paying, we verify every request by calling a number we hold independently.\"\n\n"
              f"## Challenge questions (answers are never written here)\n{qs}\n\n"
              f"## Why we are calling\n{_bullets(_top(verdict))}\n\n"
              f"Record the outcome with `resolve_case(\"{verdict.case_id}\", approve|reject, reviewer, note)`.\n")
    to = c["email"] or "(official contact not found: look it up independently)"
    email = (f"{BANNER}\n# Verification email draft: case {verdict.case_id}\n\n**To:** {to}  \n"
             f"**Never to:** {inv.sender_email or 'the sender'} (the requesting address)\n"
             f"**Subject:** Please confirm payment details for invoice {inv.invoice_number or ''}\n\n"
             f"Hello {vendor} team,\n\nWe received a request to pay {_amount(inv)} for invoice "
             f"{inv.invoice_number or '(no number)'} to an account ending {inv.bank_account_last4 or '????'}. "
             f"Our policy is to confirm payment details through an independently sourced contact before paying. "
             f"Could you confirm whether this request and these details are genuine?\n\nThank you,\nAccounts Payable\n")
    hyp = verdict.hypothesis
    ticket = (f"{BANNER}\n# Exception ticket: case {verdict.case_id}\n\n**Decision needed:** approve or reject payment "
              f"of {_amount(inv)} to {vendor}.\n**Rule:** {verdict.decision_rule} · risk {verdict.risk_score} · "
              f"confidence {verdict.confidence}\n**Hypothesis:** {hyp.title if hyp else 'none'}: "
              f"{hyp.narrative if hyp else ''}\n\n## Top evidence\n{_bullets(_top(verdict))}\n\n"
              f"## Next step\nRun the callback script, then resolve the case. Approval applies to this case only.\n")
    acts = [Action("callback_script", "Callback script", script, _write(verdict.case_id, "callback_script.md", script, inv)),
            Action("verification_email_draft", "Verification email to the official contact", email,
                   _write(verdict.case_id, "verification_email_draft.md", email, inv)),
            Action("exception_ticket", "Exception ticket", ticket,
                   _write(verdict.case_id, "exception_ticket.md", ticket, inv))]
    if _has_no_data_c1(verdict):
        acts.append(_kyb_task(verdict.case_id, inv.vendor_name, inv.vendor_domain or sender_domain(inv),
                              [e for e in verdict.evidence if e.status != "verified"], inv))
    return acts


def _kyb_task(case_id: str, vendor: Optional[str], domain: Optional[str], rows: list[Evidence],
              inv: Optional[Invoice] = None) -> Action:
    body = (f"{BANNER}\n# KYB onboarding task: {vendor or 'unknown vendor'} ({domain or 'no domain'})\n\n"
            "The vendor could not be fully verified. This is **not** proof of fraud. Before any payment, collect:\n\n"
            "1. Certificate of incorporation / company registration number (MCA / Companies House / state registry)\n"
            "2. Tax registration (GSTIN / PAN, VAT or EIN) matching the legal name\n"
            "3. Bank account proof on bank letterhead or a cancelled cheque, in the company's legal name\n"
            "4. Named finance contact reachable via the company's public switchboard\n"
            "5. Website and domain ownership consistent with the legal name\n\n"
            f"## What could not be verified\n{_bullets(rows)}\n")
    return Action("kyb_onboarding_task", "KYB onboarding task", body, _write(case_id, "kyb_onboarding_task.md", body, inv))


# ---------- BLOCK ----------

def related_cases(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    from . import gate
    out = []
    for cid in ctx.get("related_cases") or []:
        rec = gate.load_case(cid) or {"case_id": cid}
        out.append(rec)
    return out


def _case_file(verdict: Verdict, inv: Invoice, ctx: dict[str, Any], audit: Optional[str]) -> str:
    hyp = verdict.hypothesis
    related = related_cases(ctx)
    comp = ctx.get("company") if isinstance(ctx.get("company"), dict) else {}
    info = (comp or {}).get("basic_info") or {}
    offices = ((comp or {}).get("locations") or {}).get("all_office_addresses") or []
    domain = sender_domain(inv)
    timeline = [f"- {r.get('created_at', '?')}: case {r['case_id']} {r.get('decision', '')} "
                f"({r.get('title') or r.get('attack_type') or 'linked case'})" for r in related]
    timeline.append(f"- {inv.invoice_date or 'date not stated'}: email received from {inv.sender_email or 'unknown sender'}")
    timeline.append(f"- {stamp()}: PayeeGuard {verdict.decision} by {verdict.decision_rule}")
    campaign = len(related) + 1
    rel = "\n".join(f"- case {r['case_id']}: {r.get('decision', 'BLOCK')} · {r.get('title') or r.get('attack_type', '')} "
                    f"· vendor {r.get('vendor_name') or '?'} · shared account {mask_account(inv)}" for r in related) \
        or "- no related cases in the graph"
    return f"""{BANNER}
# Fraud case file: {verdict.case_id}

## 1. Summary
- Decision: **{verdict.decision}** · risk {verdict.risk_score} · rule {verdict.decision_rule}
- Amount at risk: {_amount(inv)} · claimed vendor: {inv.vendor_name or '?'} · invoice {inv.invoice_number or '?'}
- Hypothesis: **{hyp.title if hyp else 'none'}** ({hyp.attack_type if hyp else 'none'}). {hyp.narrative if hyp else ''}
- Also matched: {', '.join(h.title for h in verdict.also_matched) or 'none'}
- Ruled out: {' · '.join(r.because for r in verdict.ruled_out) or 'none'}
- Campaign: {campaign} case(s)

## 2. Timeline
{chr(10).join(timeline)}

## 3. Indicators
- Sender: {inv.sender_email or '?'} (display name: {ctx.get('display_name') or '?'})
- Sender domain: {domain or '?'} · claimed vendor domain: {inv.vendor_domain or '?'}
- Reply-To: {inv.reply_to_email or 'none'} · Return-Path: {inv.return_path_email or 'none'}
- Account: {mask_account(inv)} · SWIFT/IFSC: {inv.bank_swift or '?'}

## 4. Identity findings
- crustdata_company_id: {verdict.company_id or 'not resolved'} {('· ' + info['name']) if info.get('name') else ''}
- Domains: {', '.join(info.get('all_domains') or []) or '?'}
- Offices: {'; '.join(map(str, offices[:6])) or '?'}
- Person: {('crustdata_person_id ' + str(verdict.person_id)) if verdict.person_id else 'sender not resolved to a person'}

## 5. Graph findings (related cases, same campaign)
{rel}

## 6. Evidence ledger
{ledger_table(verdict.evidence)}

## 7. Recommended actions
1. Do not pay. Place a payment hold on the bill.
2. Notify {inv.vendor_name or 'the vendor'} via its official channel ({verdict.callback or 'look up independently'}), never via the email received.
3. Report: India **cybercrime.gov.in / 1930**; US **ic3.gov**. Include this case file.
4. Preserve the original email with full headers.

## 8. Chain of custody
- case_id {verdict.case_id} · evidence hash {evidence_hash(verdict.evidence)} · audit {audit or 'n/a'}
- generated {stamp()} · {VERSION}
"""


def _block_actions(verdict: Verdict, inv: Invoice, ctx: dict[str, Any], audit: Optional[str]) -> list[Action]:
    cf = _case_file(verdict, inv, ctx, audit)
    notice = (f"{BANNER}\n# DO NOT PAY: case {verdict.case_id}\n\nInvoice {inv.invoice_number or '?'} · "
              f"{_amount(inv)} · claimed vendor {inv.vendor_name or '?'} · account {mask_account(inv)}\n\n"
              f"Blocked by {verdict.decision_rule}. {verdict.hypothesis.narrative if verdict.hypothesis else ''}\n\n"
              "Payment through PayeeGuard's gate is refused for this case. Only `resolve_case(..., override, reviewer, "
              "note)` by an authorised reviewer can reopen it.\n")
    c = _contact(verdict, ctx)
    warn = (f"{BANNER}\n# Warning to vendor (draft): case {verdict.case_id}\n\n**To:** "
            f"{c['email'] or c['phone'] or '(official contact: look up independently)'}\n"
            f"**Subject:** Possible impersonation of {inv.vendor_name or 'your company'}\n\n"
            f"Hello,\n\nWe received a payment request in your name for {_amount(inv)} (invoice "
            f"{inv.invoice_number or '?'}) asking for payment to an account ending {inv.bank_account_last4 or '????'}, "
            f"sent from {sender_domain(inv) or 'an unknown domain'}. We have not paid it. If this did not come from you, "
            "your customers or mailbox may be targeted.\n\nAccounts Payable\n")
    return [Action("fraud_case_file", "Fraud case file", cf, _write(verdict.case_id, "fraud_case_file.md", cf, inv)),
            Action("do_not_pay_notice", "Do-not-pay notice", notice,
                   _write(verdict.case_id, "do_not_pay_notice.md", notice, inv)),
            Action("verification_email_draft", "Warning to the vendor's official contact", warn,
                   _write(verdict.case_id, "vendor_warning_draft.md", warn, inv))]


def write_actions(verdict: Verdict, inv: Invoice, ctx: dict[str, Any]) -> list[Action]:
    audit = wiring.audit_append({"event": "verdict", "case_id": verdict.case_id, "decision": verdict.decision,
                                 "rule": verdict.decision_rule, "risk": verdict.risk_score,
                                 "evidence_hash": evidence_hash(verdict.evidence)})
    ctx["audit_hash"] = audit
    if verdict.decision == "PAY":
        acts = [_payment_instruction(verdict, inv, ctx)]
        if ctx.get("watcher"):
            acts.append(Action("watcher", "Crustdata Watcher on the vendor", str(ctx["watcher"]), None, False))
        return acts
    if verdict.decision == "HOLD":
        if verdict.decision_rule.startswith(("E1", "E2")):
            return []
        return _hold_actions(verdict, inv, ctx)
    return _block_actions(verdict, inv, ctx, audit)


def write_rejected_case(verdict: Verdict, inv: Invoice, ctx: dict[str, Any]) -> list[Action]:
    """HOLD rejected by a human -> BLOCK artifacts."""
    return _block_actions(verdict, inv, ctx, ctx.get("audit_hash"))


# ---------- Mode 2 ----------

def write_onboarding_actions(result: dict[str, Any]) -> list[Action]:
    rows = [e for e in result["evidence"] if e.status != "verified"]
    if result["decision"] == "REVIEW":
        return [_kyb_task(result["case_id"], result["vendor_name"], result["domain"], rows)]
    if result["decision"] == "REJECT":
        body = (f"{BANNER}\n# Do not onboard: {result['vendor_name']} ({result['domain']})\n\n"
                f"Rejected: {result['reason']}\n\n{_bullets(rows)}\n")
        return [Action("do_not_pay_notice", "Do-not-onboard notice", body,
                       _write(result["case_id"], "do_not_onboard_notice.md", body))]
    return []
