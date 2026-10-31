"""run_all: raw email -> Verdict + finished work. Mode 2: onboard_vendor (spec 3.1)."""

import hashlib
import logging
import time
from dataclasses import asdict
from datetime import datetime

from . import __version__, audit, geo, report
from .actions import build as build_actions
from .checks_behaviour import check_c11, check_c14
from .checks_channel import check_c10, check_c2, check_c8, lookalike_reason
from .checks_company import (check_c1, check_c4, check_c5, check_c6, check_c12_tax, company_domains,
                             onboard_facts)
from .checks_payment import check_payment
from .checks_signatory import check_c3
from .comms import check_c15_relationship
from .client import CrustdataClient
from .evidence import ev, unchecked
from .extract import case_id_for, cross_check, extract
from .graph import Graph, company_node
from .investigator import investigate
from .models import Verdict
from .scoring import score

log = logging.getLogger(__name__)


def _run(fn, inv, ctx, name):
    """Edge case S10: one crashing check never kills the verdict."""
    try:
        return fn(inv, ctx)
    except Exception as e:  # noqa: BLE001
        log.exception("check %s crashed", name)
        return [unchecked(name, "CONTEXT", f"{name} ran", f"Check crashed: {type(e).__name__}", "pipeline", "api_error")]


def _early(case_id, summary, inv, ctx) -> tuple[Verdict, str]:
    v = Verdict("HOLD", 0, "LOW", decision_rule="R3 no payment request to verify", summary=summary, case_id=case_id)
    return v, report.render(v, inv, ctx)


def verify(raw_text: str, llm_fields: dict | None = None, *, client: CrustdataClient | None = None,
           graph: Graph | None = None, save: bool = True, extras: dict | None = None) -> tuple[Verdict, str]:
    """extras (optional dict) is filled with UI data: facts, invoice summary, vendor label, money-trail."""
    client = client or CrustdataClient()
    own_graph = graph is None
    graph = graph or Graph()
    calls0, hits0, credits0 = client.calls, client.cache_hits, client.credits
    try:
        return _verify(raw_text, llm_fields, client, graph, save, calls0, hits0, credits0,
                       extras if extras is not None else {})
    finally:
        if own_graph:
            graph.close()


def _verify(raw_text, llm_fields, client, graph, save, calls0, hits0, credits0, extras):
    inv, meta = extract(raw_text or "")
    inv, x1 = cross_check(inv, llm_fields)
    case_id = case_id_for(raw_text or "")
    ctx = {"client": client, "graph": graph, "meta": meta, "case_id": case_id, "display_name": meta.display_name,
           "t0": time.perf_counter(),
           "received_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "version": __version__}
    if meta.empty:
        return _early(case_id, "No content to verify.", inv, ctx)
    if meta.not_payment:
        return _early(case_id, "No payment request detected (no amount, invoice or bank details).", inv, ctx)

    evidence = list(x1)
    if meta.currency_assumed and inv.currency == "USD":
        evidence.append(ev("X3_currency_assumed", "CONTEXT", "Currency stated explicitly", "verified",
                           "Currency assumed USD from a bare '$' (could be SGD, AUD, CAD...): confirm on callback",
                           "extract:regex#currency"))
    if meta.amount_conflict:
        totals = ", ".join(f"{c} {a:,.2f}" for a, c, lab in meta.amounts if lab)
        evidence.append(ev("X2_amount_conflict", "INTEGRITY", "Amounts in the email agree", "contradicted",
                           f"Two different totals: {totals}", "extract:regex#amount", 10))

    evidence += _run(check_c1, inv, ctx, "C1_vendor")
    c1 = next(e for e in evidence if e.check == "C1_vendor")
    ctx["c1_reason"] = c1.unchecked_reason
    vdom = geo.registrable_domain(inv.vendor_domain) if inv.vendor_domain else None
    sdom = geo.email_domain(inv.sender_email)
    node = company_node(ctx.get("company_id")) or graph.company_for_domain(vdom) or graph.company_for_domain(sdom) \
        or company_node(domain=vdom or sdom)
    ctx["company_node"] = node
    ctx["vendor_known"] = graph.vendor_known(node, case_id)
    ctx["history_domains"] = graph.history_domains(node, case_id)
    ctx["fp"] = graph.fingerprint(inv.bank_iban, inv.bank_account, inv.bank_swift, inv.bank_account_last4)
    info = (ctx.get("company") or {}).get("basic_info") or {}
    ctx["vendor_label"] = f"{info.get('name')} (crustdata_company_id {ctx['company_id']})" if ctx.get("company_id") \
        else (node if ctx["vendor_known"] else None)
    ctx["real_domains"] = company_domains(ctx.get("company")) | ctx["history_domains"]

    for fn, name in ((check_c2, "C2_domain"), (check_c10, "C10_reply_to"), (check_c8, "C8_domain_age"),
                     (check_c4, "C4_shell"), (check_c12_tax, "C12_tax_id"), (check_c5, "C5_geo"), (check_c11, "C11_bank_format"),
                     (check_c14, "C14_tactics"), (check_payment, "G_payment"), (check_c15_relationship, "C15_relationship")):
        evidence += _run(fn, inv, ctx, name)
    g1 = next((e for e in evidence if e.check == "G1_account_history"), None)
    ctx["account_change"] = inv.bank_change_requested or bool(g1 and g1.status == "contradicted")
    evidence += _run(check_c3, inv, ctx, "C3_signatory")
    evidence += _run(check_c6, inv, ctx, "C6_callback")

    s = score(evidence, vendor_known=ctx["vendor_known"], amount=inv.amount, currency=inv.currency,
              bank_change=inv.bank_change_requested, credit_note=meta.credit_note)
    hyp, also, ruled = investigate(evidence)
    if c1.unchecked_reason in ("api_error", "not_enabled") and s.decision != "BLOCK":
        ctx["banner"] = "Crustdata was unavailable for part of this check: " + c1.detail
    ctx["related_cases"] = sorted({e.source.rsplit("case:", 1)[1] for e in evidence
                                   if e.check == "G3_account_flagged" and "case:" in e.source})
    if node and ctx["vendor_known"]:
        ctx["history_accounts"] = list(graph.accounts_paid(node, case_id).values())
        ctx["challenge_questions"] = _challenges(graph, node, case_id)

    v = Verdict(s.decision, s.risk, s.confidence, evidence, hyp, also, ruled, s.rule, ctx.get("company_id"),
                ctx.get("person_id"), case_id=case_id)
    cb = ctx.get("callback")
    v.callback = " / ".join(p for p in (cb.get("phone"), cb.get("email"), cb.get("website")) if p) if cb else None
    v.summary = (f"{v.decision}: {hyp.title}" if hyp else f"{v.decision}: no attack pattern") + f" ({s.rule})"
    v.crustdata_calls = client.calls - calls0
    v.crustdata_credits = round(client.credits - credits0, 2)
    ctx["cache_hits"] = client.cache_hits - hits0
    v.actions = build_actions(v, inv, ctx)
    v.graph_edges = _trail(v, inv, ctx)
    _write_graph(v, inv, ctx, graph, hyp, s)
    fp = ctx.get("fp")
    extras.update({
        "facts": s.facts, "families": s.families, "vendor_label": ctx.get("vendor_label"),
        "banner": ctx.get("banner"), "cache_hits": ctx.get("cache_hits", 0),
        "invoice": {"vendor": inv.vendor_name, "sender": inv.sender_email, "sender_name": inv.sender_name,
                    "invoice_number": inv.invoice_number, "amount": inv.amount, "currency": inv.currency,
                    "account": fp.masked if fp else None, "remit_country": inv.remit_country,
                    "bank_change": inv.bank_change_requested}})
    md = report.render(v, inv, ctx, full=False)
    if save:
        report.save(v, inv, ctx, md)
        md = report.render(v, inv, ctx, full=False)   # re-render so action paths appear
        audit.append("verified", case_id=case_id, decision=v.decision, risk=v.risk_score, rule=v.decision_rule)
    return v, md


def _trail(v: Verdict, inv, ctx) -> list[dict]:
    """Money-trail edges for the UI: sender -> invoice -> vendor / account -> history / blocked cases."""
    by = {e.check: e for e in v.evidence}
    st = lambda c: by[c].status if c in by else None  # noqa: E731
    sender_state = "bad" if st("C2_lookalike") == "contradicted" or st("C2b_freemail") == "contradicted" \
        else "ok" if st("C2_domain") == "verified" else "unknown"
    vendor = ctx.get("vendor_label") or inv.vendor_name or "vendor?"
    edges = []
    if inv.sender_email:
        edges.append({"src": "sender", "src_label": inv.sender_email.split("@")[1], "rel": "SENT", "dst": "invoice",
                      "dst_label": inv.invoice_number or "invoice", "state": sender_state})
    edges.append({"src": "invoice", "src_label": inv.invoice_number or "invoice", "rel": "CLAIMS_VENDOR",
                  "dst": "vendor", "dst_label": vendor, "state": "ok" if st("C1_vendor") == "verified" else "unknown"})
    fp = ctx.get("fp")
    if fp:
        acct_state = "bad" if st("G3_account_flagged") == "contradicted" else \
            "warn" if st("G1_account_history") == "contradicted" or st("G2_account_other_vendor") == "contradicted" \
            else "ok" if st("G1_account_history") == "verified" else "unknown"
        edges.append({"src": "invoice", "src_label": inv.invoice_number or "invoice", "rel": "PAYS_TO",
                      "dst": "account", "dst_label": fp.masked, "state": acct_state})
        for i, c in enumerate(ctx.get("related_cases") or []):
            edges.append({"src": "account", "src_label": fp.masked, "rel": "FLAGGED_IN", "dst": f"case{i}",
                          "dst_label": f"blocked case {c}", "state": "bad"})
    for p in (ctx.get("history_accounts") or [])[:2]:
        edges.append({"src": "vendor", "src_label": vendor, "rel": f"PAID ×{p['n']}", "dst": f"hist_{p['last4']}",
                      "dst_label": f"••••{p['last4']} {p['country'] or ''}".strip(), "state": "history"})
    return edges


def _challenges(graph, node, case_id) -> list[str]:
    rows = graph.payments_for(node, case_id)
    if not rows:
        return []
    last = rows[-1]
    return [f"What was the amount of invoice {last['invoice_no']} that we paid on {last['paid_on']}? "
            "(answer is in our ledger; never put it in the email)"]


def _write_graph(v: Verdict, inv, ctx, graph: Graph, hyp, s) -> None:
    case_id, node, fp = v.case_id, ctx["company_node"], ctx.get("fp")
    inv_node = graph.node(f"invoice:{node}:{inv.invoice_number or case_id}", "invoice", inv.invoice_number or case_id)
    if node:
        graph.node(node, "company", ctx.get("vendor_label") or node)
        graph.edge(inv_node, "CLAIMS_VENDOR", node, "extract+C1", case_id)
    if fp:
        graph.edge(inv_node, "PAYS_TO", graph.account(fp), "extract:regex#bank", case_id)
    sender_node = None
    if inv.sender_email:
        sender_node = graph.node("email:" + hashlib.sha1(inv.sender_email.encode()).hexdigest()[:16], "email",
                                 inv.sender_email.split("@")[1])
        graph.edge(sender_node, "SENT", inv_node, "extract:header#From", case_id)
    data = {"vendor": inv.vendor_name, "company_node": node, "company_id": ctx.get("company_id"),
            "invoice_number": inv.invoice_number, "amount": inv.amount, "currency": inv.currency,
            "sender_domain": geo.email_domain(inv.sender_email), "sender_node": sender_node,
            "fp": asdict(fp) if fp else None, "rule": v.decision_rule, "risk": v.risk_score,
            "attack": hyp.attack_type if hyp else "none", "ms": round((time.perf_counter() - ctx["t0"]) * 1000),
            "credits": v.crustdata_credits,
            "top_reason": next((e.detail for e in sorted((e for e in v.evidence if e.status == "contradicted"),
                                                         key=lambda e: (not e.hard_block, -e.weight))), v.decision_rule)}
    old = graph.get_case(case_id)
    if old and old["state"] == "APPROVED" and v.decision == "BLOCK":
        data["approvals"] = old["data"].get("approvals", [])
        data["voided"] = "approval voided: new hard evidence on re-verification"
        graph.save_case(case_id, v.decision, "BLOCK", data)          # a new BLOCK voids an earlier human approval
    elif old and old["state"] in ("APPROVED", "RELEASED"):
        data["approvals"] = old["data"].get("approvals", [])
        graph.save_case(case_id, v.decision, old["state"], data)    # keep human decisions on re-run
    else:
        graph.save_case(case_id, v.decision, v.decision, data)
    if v.decision == "BLOCK":
        flags = [n for n in ((fp.id if fp and fp.strong else None), sender_node) if n]
        graph.flag(case_id, flags, f"verdict:BLOCK@case:{case_id} ({v.decision_rule})")
    graph.commit()


# ---- Mode 2 -------------------------------------------------------------------------------------------

def onboard_vendor(vendor_name: str, website: str, claims: dict | None = None, *,
                   client: CrustdataClient | None = None, graph: Graph | None = None,
                   extras: dict | None = None) -> tuple[str, str]:
    client = client or CrustdataClient()
    own = graph is None
    graph = graph or Graph()
    try:
        f = onboard_facts(client, vendor_name, website, claims)
        domain = f["domain"]
        reasons, decision = list(f["off"]), "APPROVE"
        real = f.get("real_domain")
        why = lookalike_reason(domain, real) if domain and real and domain != real else None
        if not why and domain and f["cid"] is None and f.get("brand"):
            # Only a vendor we already know by this name (graph) counts; a site containing its own name is normal.
            for d in sorted(graph.known_domains_for_brand(geo.skeleton(f["brand"]))):
                why = lookalike_reason(domain, d)
                if why:
                    real = d
                    break
        if why:
            decision, reasons = "REJECT", [f"Website imitates {real}: {why}"] + reasons
        if decision != "REJECT" and f["cid"] is None:
            decision = "REVIEW"
            reasons.insert(0, f"No company in Crustdata uses {domain}" if domain else "No valid website given")
            try:
                dns = client.dns(domain, "A")
                if isinstance(dns, dict) and dns.get("Status") == 3:
                    reasons.append(f"{domain} does not exist in DNS")
            except Exception:  # noqa: BLE001
                pass
        elif decision != "REJECT" and reasons:
            decision = "REVIEW"
        if f["errors"] and decision == "APPROVE":
            decision, reasons = "REVIEW", ["Crustdata unavailable: " + "; ".join(f["errors"])] + reasons
        if decision == "APPROVE":
            node = graph.node(company_node(f["cid"]), "company", vendor_name)
            graph.edge(node, "ONBOARDED", graph.node(f"domain:{domain}", "domain", domain),
                       f"onboard_vendor@crustdata:company/{f['cid']}")
            graph.commit()
        audit.append("onboarded", vendor=vendor_name, domain=domain, decision=decision)
        if extras is not None:
            extras.update({"rows": f["rows"], "reasons": reasons, "contact": f.get("contact"),
                           "leaders": [{"name": n, "title": t, "since": s, "person_id": p}
                                       for n, t, s, p in f["leaders"][:8]], "domain": domain, "cid": f["cid"]})
        lines = [f"# Vendor Trust Report: {decision}", f"{vendor_name} · {website}", "",
                 "| Question | Answer | Source |", "|---|---|---|"]
        lines += [f"| {q} | {a} | `{s}` |" for q, a, s in f["rows"]]
        lines += ["", "## Anything off?"] + ([f"- {r}" for r in reasons] or ["- Nothing found"])
        c = f.get("contact")
        if c:
            lines += ["", f"Official contact (Crustdata): {c.get('phone') or '-'} / {c.get('email') or '-'}"]
        nxt = {"APPROVE": "Vendor added to the vendor master; future invoices from this domain are trusted as its channel.",
               "REVIEW": "A human must collect KYB documents (registration, tax ID, bank letter) before onboarding.",
               "REJECT": "Do not onboard. This looks like impersonation of another company."}[decision]
        lines += ["", f"**Next step:** {nxt}"]
        return decision, "\n".join(lines)
    finally:
        if own:
            graph.close()
