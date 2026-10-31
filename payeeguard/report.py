"""Markdown + JSON verdict report with a Mermaid money trail (spec 12), and the Mode 2 Vendor Trust Report."""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import reports_dir
from .extract import IBAN, _best_iban, iban_valid, mask_account
from .models import Evidence, Invoice, Verdict

IST = timezone(timedelta(hours=5, minutes=30), "IST")
CHAT_ROWS = 12
CHAT_CHARS = 15_000
STATUS_ORDER = {"contradicted": 0, "unchecked": 1, "verified": 2}


def stamp() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


# ---------- privacy ----------

def redact(text: str, inv: Optional[Invoice] = None) -> str:
    """Y2: full IBANs / account numbers never leave memory, whoever wrote the text."""
    secrets = [s for s in ((inv.bank_iban, inv.bank_account) if inv else ()) if s and len(s) >= 6]
    for s in secrets:
        spaced = r"[\s\-]?".join(map(re.escape, s))
        text = re.sub(spaced, "••" + s[-4:], text, flags=re.I)

    def _iban(m: re.Match) -> str:
        iban = _best_iban(m.group(1))            # the regex can run into the next word
        if not iban_valid(iban):
            return m.group(0)
        return "••" + re.sub(r"\s", "", iban)[-4:] + m.group(1)[len(iban):]
    return IBAN.sub(_iban, text)


def cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").strip()


# ---------- pieces ----------

def ledger(evidence: list[Evidence]) -> list[Evidence]:
    """Contradicted first (hard, then heaviest), then unchecked, then verified; stable within ties."""
    return sorted(evidence, key=lambda e: (STATUS_ORDER.get(e.status, 3), not e.hard_block, -e.weight, e.check))


def _result(e: Evidence) -> str:
    if e.status == "contradicted":
        return "CONTRADICTED (hard)" if e.hard_block else f"CONTRADICTED +{e.weight}"
    if e.status == "unchecked":
        return f"unchecked ({e.unchecked_reason or 'unknown'})"
    return "verified" + (f" ✔{e.confirms}" if e.confirms else "")


def ledger_table(evidence: list[Evidence], limit: Optional[int] = None) -> str:
    rows = ledger(evidence)
    shown = rows[:limit] if limit else rows
    out = ["| # | Family | Check | Claim | Result | Detail | Source |", "|---|---|---|---|---|---|---|"]
    for i, e in enumerate(shown, 1):
        out.append(f"| {i} | {e.family} | {cell(e.check)} | {cell(e.claim)} | {_result(e)} | {cell(e.detail)} | "
                   f"{cell(e.source)} |")
    if limit and len(rows) > limit:
        out.append(f"\n_{len(rows) - limit} more rows in the full report file._")
    return "\n".join(out)


def _mm(label: str) -> str:
    return '"' + re.sub(r'["\[\]{}()<>|]', " ", label).strip().replace("  ", " ") + '"'


def mermaid(verdict: Verdict, case_titles: Optional[dict[str, str]] = None) -> str:
    if not verdict.graph_edges:
        return ""
    ids: dict[str, str] = {}
    lines = ["graph LR"]
    vendor_ok = any(e.check.startswith("C1") and e.status == "verified" for e in verdict.evidence)

    def node(key: str, label: str) -> str:
        if key not in ids:
            ids[key] = f"N{len(ids)}"
            if key.startswith("company:") and vendor_ok:
                label += " ✔"
            if key.startswith("case:"):
                cid = key.removeprefix("case:")
                label = f"Case {cid[:4]}…: {(case_titles or {}).get(cid, 'prior case')} BLOCKED"
            lines.append(f"  {ids[key]}[{_mm(label)}]")
        return ids[key]

    for e in verdict.graph_edges:
        if not e.get("src") or not e.get("dst"):
            continue
        a, b = node(e["src"], e.get("src_label", e["src"])), node(e["dst"], e.get("dst_label", e["dst"]))
        if e["rel"] == "PAID":
            lines.append(f"  {a} -. PAID x{e.get('count', 1)} .-> {b}")
        else:
            lines.append(f"  {a} -->|{e['rel']}| {b}")
    return "\n".join(lines)


def _company_line(ctx: dict[str, Any], verdict: Verdict) -> Optional[str]:
    comp = ctx.get("company") if isinstance(ctx.get("company"), dict) else None
    if not comp and not verdict.company_id:
        return None
    info = (comp or {}).get("basic_info") or {}
    offices = ((comp or {}).get("locations") or {}).get("all_office_addresses") or []
    countries = sorted({str(o).rsplit(",", 1)[-1].strip() for o in offices if isinstance(o, str) and "," in o})
    parts = [f"Resolved vendor: {info.get('name') or 'company'} (crustdata_company_id {verdict.company_id})"]
    if info.get("primary_domain"):
        parts.append(str(info["primary_domain"]))
    if countries:
        parts.append("offices " + ", ".join(countries[:8]))
    return " · ".join(parts)


def _case_titles(ctx: dict[str, Any]) -> dict[str, str]:
    from . import gate
    out = {}
    for cid in ctx.get("related_cases") or []:
        rec = gate.load_case(cid)
        if rec:
            out[cid] = rec.get("title") or rec.get("attack_type") or "prior case"
    return out


def safe_next_step(verdict: Verdict, inv: Invoice) -> str:
    contact = f"{verdict.callback} (found independently, not from the email)" if verdict.callback else \
        "the vendor's official contact, looked up independently (never the details in this email)"
    if verdict.decision == "PAY":
        return f"Release through the payment gate: `pay_invoice(\"{verdict.case_id}\")` (SIMULATED)."
    if verdict.decision == "HOLD":
        return (f"Do not pay yet. Call {contact} and use the challenge questions in callback_script.md, then "
                f"`resolve_case(\"{verdict.case_id}\", approve|reject, reviewer, note)`.")
    return f"Do not pay. Warn {inv.vendor_name or 'the vendor'} via {contact}; preserve the email headers."


def render(verdict: Verdict, inv: Invoice, ctx: dict[str, Any], limit: Optional[int] = None,
           report_path: Optional[str] = None) -> str:
    hyp = verdict.hypothesis
    out = [f"# PayeeGuard verdict: {verdict.decision} · risk {verdict.risk_score} · rule {verdict.decision_rule} · "
           f"case {verdict.case_id}", ""]
    if ctx.get("banner"):
        out += [f"> ⚠ {ctx['banner']}", ""]
    bits = [inv.invoice_number and f"Invoice {inv.invoice_number}", inv.vendor_name,
            inv.amount is not None and f"{inv.currency or '???'} {inv.amount:,.2f}",
            inv.sender_email and f"from {inv.sender_email}",
            (inv.bank_iban or inv.bank_account) and f"to account {mask_account(inv)}",
            inv.bank_change_requested and "bank change requested"]
    out.append(" · ".join(b for b in bits if b) or "No invoice details extracted")
    company = _company_line(ctx, verdict)
    if company:
        out.append(company)
    out += ["", "## Investigation"]
    if hyp:
        title = f"**Most likely: {hyp.title} ({hyp.attack_type})"
        if verdict.also_matched:
            title += " + " + " + ".join(h.title.lower() for h in verdict.also_matched)
        out += [title + "**", hyp.narrative]
    if verdict.ruled_out:
        out.append("Ruled out: " + " · ".join(r.because for r in verdict.ruled_out))
    out.append(f"Summary: {verdict.summary}")
    chart = mermaid(verdict, _case_titles(ctx))
    if chart:
        out += ["", "## Money trail", "```mermaid", chart, "```"]
    out += ["", "## Evidence ledger   (contradicted first, then unchecked, then verified)",
            ledger_table(verdict.evidence, limit) if verdict.evidence else "_No checks ran._"]
    if verdict.actions:
        out += ["", "## Finished work"]
        out += [f"- {a.title}" + (f" → `{a.path}`" if a.path else "") + (" (SIMULATED / DRAFT)" if a.simulated else "")
                for a in verdict.actions]
    out += ["", "## Safe next step", safe_next_step(verdict, inv), ""]
    if report_path:
        out.append(f"Full report: `{report_path}`")
    out.append(f"Confidence {verdict.confidence} · {verdict.crustdata_calls} Crustdata calls · "
               f"{verdict.crustdata_credits:g} credits · {stamp()}")
    return redact("\n".join(out), inv)


def verdict_json(verdict: Verdict, inv: Invoice) -> dict[str, Any]:
    data = asdict(verdict)
    data["invoice"] = {"vendor_name": inv.vendor_name, "invoice_number": inv.invoice_number, "amount": inv.amount,
                       "currency": inv.currency, "sender_email": inv.sender_email, "account": mask_account(inv),
                       "remit_country": inv.remit_country, "bank_change_requested": inv.bank_change_requested}
    return json.loads(redact(json.dumps(data, ensure_ascii=False), inv))


def case_dir(case_id: str) -> Path:
    d = reports_dir() / case_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_report(verdict: Verdict, inv: Invoice, ctx: dict[str, Any]) -> Path:
    d = case_dir(verdict.case_id)
    path = d / "report.md"
    path.write_text(render(verdict, inv, ctx), encoding="utf-8")
    (d / "verdict.json").write_text(json.dumps(verdict_json(verdict, inv), indent=2, ensure_ascii=False),
                                    encoding="utf-8")
    return path


def chat_report(verdict: Verdict, inv: Invoice, ctx: dict[str, Any]) -> str:
    """M5: top rows in chat, full ledger in the file, capped at ~15k chars."""
    path = reports_dir() / verdict.case_id / "report.md"
    text = render(verdict, inv, ctx, limit=CHAT_ROWS, report_path=str(path))
    if len(text) > CHAT_CHARS:
        text = text[:CHAT_CHARS - 200].rsplit("\n", 1)[0] + f"\n\n_…truncated; full report: `{path}`_"
    return text


# ---------- Mode 2 ----------

def render_onboarding(result: dict[str, Any]) -> str:
    facts = result.get("facts") or {}
    out = [f"# Vendor Trust Report: {result['decision']} · {result.get('vendor_name') or '?'} · "
           f"{result.get('domain') or '?'} · case {result['case_id']}", "", f"Decision reason: {result['reason']}", ""]
    q = [("Is it real?", facts.get("company_id") and f"crustdata_company_id {facts['company_id']}"
          + (f" ({facts.get('name')})" if facts.get("name") else "")),
         ("How long has it existed?", facts.get("year_founded") and f"founded {facts['year_founded']}"
          + (f"; domain registered {facts['domain_registered']}" if facts.get("domain_registered") else "")),
         ("Is it trading?", facts.get("headcount") and f"headcount {facts['headcount']}"
          + (f", {facts['openings']} open roles" if facts.get("openings") is not None else "")),
         ("Official contact", " · ".join(str(facts[k]) for k in ("phone", "email") if facts.get(k)) or None)]
    out += ["## Answers"] + [f"- **{k}** {v or 'not verified'}" for k, v in q]
    leaders = facts.get("leaders") or []
    if leaders:
        out += ["", "## Who runs it", "| Name | Title | Since | Person id |", "|---|---|---|---|"]
        out += [f"| {cell(p.get('name'))} | {cell(p.get('title'))} | {cell(p.get('start_date'))} | "
                f"{cell(p.get('person_id') or p.get('crustdata_person_id'))} |" for p in leaders[:12]]
    off = [e for e in result["evidence"] if e.status == "contradicted"]
    out += ["", "## Anything off?"] + ([f"- {e.check}: {e.detail}" for e in off] or ["- Nothing contradicted."])
    out += ["", "## Evidence ledger", ledger_table(result["evidence"]) if result["evidence"] else "_No checks ran._"]
    if result.get("actions"):
        out += ["", "## Finished work"] + [f"- {a.title} → `{a.path}`" for a in result["actions"]]
    out += ["", stamp()]
    return "\n".join(out)


def write_onboarding_report(result: dict[str, Any]) -> Path:
    path = case_dir(result["case_id"]) / "vendor_trust_report.md"
    path.write_text(render_onboarding(result), encoding="utf-8")
    return path
