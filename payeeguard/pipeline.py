"""run_all(raw_text, llm_fields) -> Verdict. Extract, run every check isolated, score, investigate, finish the work."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from . import wiring
from .config import policy
from .extract import case_id_for, cross_check, extract
from .investigator import investigate
from .models import Evidence, Invoice, Verdict
from .scoring import decide, decide_onboarding, has

log = logging.getLogger(__name__)
_UNSET: Any = object()


@dataclass
class Case:
    verdict: Verdict
    invoice: Invoice
    ctx: dict[str, Any] = field(default_factory=dict)


def evidence_hash(evidence: list[Evidence]) -> str:
    blob = json.dumps([asdict(e) for e in evidence], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def email_node(email: Optional[str]) -> Optional[str]:
    return "email:" + hashlib.sha1(email.encode()).hexdigest()[:16] if email else None


def sender_domain(inv: Invoice) -> Optional[str]:
    return inv.sender_email.split("@", 1)[1] if inv.sender_email and "@" in inv.sender_email else None


def invoice_node(inv: Invoice, ctx: dict[str, Any]) -> Optional[str]:
    if not inv.invoice_number:
        return None
    vendor = ctx.get("company_id") or inv.vendor_domain or (inv.vendor_name or "unknown").lower()
    return f"invoice:{vendor}:{inv.invoice_number}"


def company_node(ctx: dict[str, Any]) -> Optional[str]:
    return f"company:{ctx['company_id']}" if ctx.get("company_id") else None


# ---------- checks ----------

def _crash_row(name: str, family: str, err: BaseException) -> Evidence:
    return Evidence(name, family, f"{name} ran", "unchecked", f"check failed ({type(err).__name__}); not counted as risk",
                    "pipeline", unchecked_reason="api_error")  # type: ignore[arg-type]


def run_checks(inv: Invoice, ctx: dict[str, Any], client: Any, graph: Any,
               checks: Optional[list] = None) -> list[Evidence]:
    """S10: each check is isolated; an exception becomes one unchecked/api_error row."""
    out: list[Evidence] = []
    for name, family, fn in (wiring.load_checks() if checks is None else checks):
        try:
            rows = fn(inv, ctx, client, graph) or []
            out += [r for r in rows if isinstance(r, Evidence)]
        except Exception as e:  # noqa: BLE001 - isolation is the point
            log.warning("check %s failed: %s", name, type(e).__name__)
            out.append(_crash_row(name, family, e))
    return out


def _graph_call(graph: Any, method: str, *args, default: Any = None, **kw) -> Any:
    fn = getattr(graph, method, None) if graph is not None else None
    if fn is None:
        return default
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001 - graph trouble must not change the verdict path
        log.warning("graph.%s failed: %s", method, type(e).__name__)
        return default


def _graph_context(inv: Invoice, ctx: dict[str, Any], graph: Any, evidence: list[Evidence]) -> list[Evidence]:
    """Ledger facts the pipeline owns: vendor_known, account on file (E11), identity edges (G-2)."""
    case_id = ctx["case_id"]
    acct = _graph_call(graph, "account_node", inv)
    ctx["account_node"] = acct
    comp = company_node(ctx)
    paid = _graph_call(graph, "accounts_paid", comp, exclude_case=case_id, default={}) if comp else {}
    ctx["ledger_accounts"] = dict(sorted((paid or {}).items(), key=lambda kv: (-kv[1], kv[0])))
    ctx.setdefault("vendor_known", bool(paid))
    extra: list[Evidence] = []
    if not acct and ctx["flags"].get("account_on_file") and paid and not has(evidence, "G1", "verified"):
        ledger, n = next(iter(ctx["ledger_accounts"].items()))
        ctx["ledger_account"] = ledger
        extra.append(Evidence(
            "E11_account_on_file", "PAYMENT", "Payment goes to the account on file", "verified",
            f"no account in the email; the payment instruction uses the ledger account ({n} prior payments)",
            f"graph:PAID/{comp}->{ledger}", confirms="money"))
    if acct:
        ctx["related_cases"] = _graph_call(graph, "cases_flagged", acct, exclude_case=case_id, default=[]) or []

    inv_n, mail = invoice_node(inv, ctx), email_node(inv.sender_email)
    if inv_n and mail:
        _graph_call(graph, "add_edge", mail, "SENT", inv_n, "extract:regex#sender_email", case_id)
    if inv_n and comp:
        _graph_call(graph, "add_edge", inv_n, "CLAIMS_VENDOR", comp, f"crustdata:{comp.replace(':', '/')}#basic_info",
                    case_id)
    if inv_n and acct:
        _graph_call(graph, "add_edge", inv_n, "PAYS_TO", acct, "extract:regex#bank_account", case_id)
    return extra


def _flag_block(inv: Invoice, ctx: dict[str, Any], graph: Any, evidence: list[Evidence]) -> None:
    """FLAGGED_IN on BLOCK. The vendor's own verified domain/mailbox is never flagged, only the account."""
    case_node, cid = f"case:{ctx['case_id']}", ctx["case_id"]
    cite = f"pipeline:BLOCK@case:{cid}"
    if ctx.get("account_node") and not str(ctx["account_node"]).startswith("account:weak:"):
        _graph_call(graph, "add_edge", ctx["account_node"], "FLAGGED_IN", case_node, cite, cid)
    if not has(evidence, "C2", "verified"):
        dom = sender_domain(inv)
        if dom:
            _graph_call(graph, "add_edge", f"domain:{dom}", "FLAGGED_IN", case_node, cite, cid)
        if email_node(inv.sender_email):
            _graph_call(graph, "add_edge", email_node(inv.sender_email), "FLAGGED_IN", case_node, cite, cid)


def _money_trail(inv: Invoice, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    from .extract import mask_account
    edges: list[dict[str, Any]] = []
    inv_n, comp, acct = invoice_node(inv, ctx) or "invoice:(no number)", company_node(ctx), ctx.get("account_node")
    name = (ctx.get("company") or {}).get("basic_info", {}).get("name") if isinstance(ctx.get("company"), dict) else None
    labels = {inv_n: inv.invoice_number or "Invoice", comp: name or inv.vendor_name or comp}
    if comp:
        edges.append({"src": inv_n, "rel": "CLAIMS_VENDOR", "dst": comp, "citation": f"crustdata:{comp.replace(':', '/')}"})
    if acct:
        labels[acct] = f"Account {mask_account(inv)}"
        edges.append({"src": inv_n, "rel": "PAYS_TO", "dst": acct, "citation": "extract:regex#bank_account"})
    for other, n in (ctx.get("ledger_accounts") or {}).items():
        labels.setdefault(other, "Ledger account")
        edges.append({"src": comp, "rel": "PAID", "dst": other, "count": n, "citation": f"graph:PAID/{comp}->{other}"})
    for case in ctx.get("related_cases") or []:
        edges.append({"src": acct, "rel": "FLAGGED_IN", "dst": f"case:{case}",
                      "citation": f"graph:FLAGGED_IN/{acct}->case:{case}"})
    for e in edges:
        e["src_label"] = labels.get(e["src"], e["src"])
        e["dst_label"] = labels.get(e["dst"], e["dst"])
    return edges


# ---------- decision ----------

def assess(inv: Invoice, evidence: list[Evidence], ctx: dict[str, Any]) -> Verdict:
    """Pure: evidence -> Verdict (used by run_all and by ablation)."""
    score = decide(inv, evidence, ctx)
    hyp, also, ruled = investigate(evidence, inv, low_confidence=score.confidence == "LOW")
    summary = f"{score.decision}: {hyp.title}"
    if hyp.attack_type != "none":
        summary += f" ({hyp.attack_type})"
    notes = list(ctx.get("summary_notes", [])) + score.notes
    if score.kyb_task and score.decision != "BLOCK":
        notes.append("vendor not found in Crustdata: KYB onboarding task created")
    if notes:
        summary += ". " + "; ".join(notes)
    callback = ctx.get("callback")
    if isinstance(callback, dict):
        callback = " · ".join(str(callback[k]) for k in ("phone", "email", "url") if callback.get(k)) or None
    return Verdict(score.decision, score.risk, score.confidence, evidence=evidence, hypothesis=hyp, also_matched=also,
                   ruled_out=ruled, decision_rule=score.rule, company_id=ctx.get("company_id"),
                   person_id=ctx.get("person_id"), callback=callback, summary=summary, case_id=ctx["case_id"],
                   graph_edges=_money_trail(inv, ctx))


def _short_circuit(inv: Invoice, ctx: dict[str, Any], rule: str, summary: str) -> Verdict:
    from .models import Hypothesis
    return Verdict("HOLD", 0, "LOW", decision_rule=rule, summary=f"HOLD: {summary}", case_id=ctx["case_id"],
                   hypothesis=Hypothesis("none", "No attack pattern detected", summary))


def analyse(raw_text: str, llm_fields: Optional[dict[str, Any]] = None, *, client: Any = _UNSET, graph: Any = _UNSET,
            checks: Optional[list] = None) -> Case:
    ex = cross_check(extract(raw_text or ""), llm_fields)
    inv = ex.invoice
    ctx: dict[str, Any] = {"case_id": case_id_for(raw_text or ""), "policy": policy(), "flags": ex.flags,
                           "credit_note": ex.flags.get("credit_note", False), "summary_notes": list(ex.notes),
                           "display_name": ex.flags.get("display_name"), "body": ex.flags.get("body", "")}
    if ex.flags.get("empty"):
        return Case(_short_circuit(inv, ctx, "E1 no content", "no content"), inv, ctx)
    if ex.flags.get("not_payment"):
        return Case(_short_circuit(inv, ctx, "E2 no payment request", "no payment request detected"), inv, ctx)

    client = wiring.make_client() if client is _UNSET else client
    graph = wiring.open_graph() if graph is _UNSET else graph
    ctx["graph"] = graph
    banner = getattr(client, "banner", None)
    if not banner and not os.environ.get("CRUSTDATA_API_KEY") and os.environ.get("PAYEEGUARD_OFFLINE") != "1":
        banner = "Crustdata key missing/invalid: identity checks could not run"
    if banner:
        ctx["banner"] = banner
    if checks is None and not wiring.checks_available():
        ctx["banner"] = (ctx.get("banner", "") + " · " if ctx.get("banner") else "") + \
            "verification checks not installed: decision is fail-safe only"

    evidence = list(ex.evidence) + run_checks(inv, ctx, client, graph, checks)
    evidence += _graph_context(inv, ctx, graph, evidence)
    verdict = assess(inv, evidence, ctx)
    verdict.crustdata_calls, verdict.crustdata_credits = wiring.client_counters(client)
    if verdict.decision == "BLOCK":
        _flag_block(inv, ctx, graph, evidence)
    return Case(verdict, inv, ctx)


def run_all(raw_text: str, llm_fields: Optional[dict[str, Any]] = None, *, client: Any = _UNSET, graph: Any = _UNSET,
            checks: Optional[list] = None, write: bool = True) -> Verdict:
    case = analyse(raw_text, llm_fields, client=client, graph=graph, checks=checks)
    if write:
        finish(case)
    return case.verdict


def finish(case: Case) -> None:
    """Finished work + report files + case record for the gate."""
    from . import actions, gate, report
    case.verdict.actions = actions.write_actions(case.verdict, case.invoice, case.ctx)
    report.write_report(case.verdict, case.invoice, case.ctx)
    gate.save_case(case.verdict, case.invoice, case.ctx)


# ---------- Mode 2: vendor onboarding ----------

def website_domain(website: str) -> str:
    w = (website or "").strip().lower()
    host = urlparse(w if "://" in w else "https://" + w).hostname or ""
    return re.sub(r"^www\.", "", host).rstrip(".")


def onboard(vendor_name: str, website: str, claimed_country: Optional[str] = None,
            claimed_headcount: Optional[int] = None, claimed_offices: Optional[int] = None,
            *, client: Any = _UNSET, graph: Any = _UNSET, write: bool = True) -> dict[str, Any]:
    domain = website_domain(website)
    case_id = hashlib.sha256(f"onboard|{(vendor_name or '').strip().lower()}|{domain}".encode()).hexdigest()[:12]
    client = wiring.make_client() if client is _UNSET else client
    graph = wiring.open_graph() if graph is _UNSET else graph
    fn = wiring.onboard_facts_fn()
    evidence: list[Evidence] = []
    facts: dict[str, Any] = {}
    if not vendor_name or not domain:
        evidence.append(Evidence("O0_input", "CONTEXT", "Vendor name and website given", "unchecked",
                                 "vendor name or website missing", "input", unchecked_reason="no_data"))
    elif fn is None:
        evidence.append(Evidence("C1_vendor", "COMPANY", "Vendor exists in Crustdata", "unchecked",
                                 "onboarding checks not installed", "pipeline", unchecked_reason="not_enabled"))
    else:
        try:
            res = fn(vendor_name=vendor_name, website=website, client=client, claimed_country=claimed_country,
                     claimed_headcount=claimed_headcount, claimed_offices=claimed_offices)
            if isinstance(res, tuple):
                evidence, facts = list(res[0]), dict(res[1] or {})
            elif isinstance(res, dict):
                evidence, facts = list(res.get("evidence", [])), {k: v for k, v in res.items() if k != "evidence"}
            else:
                evidence = list(res or [])
        except Exception as e:  # noqa: BLE001
            log.warning("onboard_facts failed: %s", type(e).__name__)
            evidence.append(_crash_row("onboard_facts", "COMPANY", e))
    decision, reason = decide_onboarding(evidence)
    result = {"case_id": case_id, "vendor_name": vendor_name, "website": website, "domain": domain,
              "decision": decision, "reason": reason, "evidence": evidence, "facts": facts,
              "claimed": {"country": claimed_country, "headcount": claimed_headcount, "offices": claimed_offices}}
    if decision == "APPROVE" and facts.get("company_id"):
        _graph_call(graph, "add_edge", f"company:{facts['company_id']}", "ONBOARDED", f"domain:{domain}",
                    f"crustdata:company/{facts['company_id']}#basic_info.all_domains", case_id)
    if write:
        from . import actions, report
        result["actions"] = actions.write_onboarding_actions(result)
        report.write_onboarding_report(result)
    return result
