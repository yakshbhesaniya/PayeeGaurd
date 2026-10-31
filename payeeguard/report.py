"""Markdown + JSON report (spec section 12). Verdict first, contradicted rows first, sources on every row."""

import json
import re
from dataclasses import asdict
from pathlib import Path

from . import config
from .models import Verdict

ORDER = {"contradicted": 0, "unchecked": 1, "verified": 2}
CHAT_ROWS = 14
MAX_CHARS = 15_000


def _mermaid(verdict: Verdict, inv, ctx) -> str:
    vendor = (ctx.get("vendor_label") or inv.vendor_name or "vendor").replace('"', "'")
    ok = "✔" if any(e.check == "C1_vendor" and e.status == "verified" for e in verdict.evidence) else "?"
    lines = ["```mermaid", "graph LR", f'  INV["{inv.invoice_number or "invoice"}"] -->|CLAIMS_VENDOR| V["{vendor} {ok}"]']
    fp = ctx.get("fp")
    if fp:
        lines.append(f'  INV -->|PAYS_TO| ACC["Account {fp.masked}"]')
    for p in (ctx.get("history_accounts") or [])[:2]:
        lines.append(f'  V -. "PAID x{p["n"]}" .-> H{p["last4"]}["Account ••••{p["last4"]} {p["country"] or ""}"]')
    for i, c in enumerate(ctx.get("related_cases") or []):
        lines.append(f'  ACC -->|FLAGGED_IN| C{i}["Blocked case {c}"]')
    lines.append("```")
    return "\n".join(lines)


def render(verdict: Verdict, inv, ctx, full: bool = True) -> str:
    hyp = verdict.hypothesis
    head = f"# PayeeGuard verdict: {verdict.decision} · risk {verdict.risk_score} · confidence {verdict.confidence}"
    amount = f"{inv.currency or ''} {inv.amount:,.2f}".strip() if inv.amount is not None else "amount n/a"
    out = [head, f"Rule: {verdict.decision_rule} · case `{verdict.case_id}`", "",
           f"Invoice {inv.invoice_number or '?'} · {inv.vendor_name or 'vendor ?'} · {amount}"
           + (" · bank change requested" if inv.bank_change_requested else "")]
    if ctx.get("vendor_label"):
        out.append(f"Resolved vendor: {ctx['vendor_label']}")
    if ctx.get("banner"):
        out.append(f"\n**⚠ {ctx['banner']}**")
    out += ["", "## Investigation"]
    if hyp:
        out.append(f"**Most likely: {hyp.title}** (`{hyp.attack_type}`)")
        out.append(hyp.narrative)
        if verdict.also_matched:
            out.append("Also consistent with: " + ", ".join(f"{h.title} (`{h.attack_type}`)" for h in verdict.also_matched))
    else:
        out.append("No attack pattern detected." if verdict.decision == "PAY" else
                   "No attack pattern detected; held because key facts could not be verified (missing data is not fraud).")
    if verdict.ruled_out:
        out.append("Ruled out: " + " · ".join(f"{r.attack_type} ({r.because})" for r in verdict.ruled_out))
    out += ["", "## Money trail", _mermaid(verdict, inv, ctx), "",
            "## Evidence ledger (contradicted first, then unchecked, then verified)",
            "| # | Family | Check | Claim | Result | Detail | Source |", "|---|---|---|---|---|---|---|"]
    rows = sorted(verdict.evidence, key=lambda e: (ORDER[e.status], -e.weight))
    shown = rows if full else rows[:CHAT_ROWS]
    for i, e in enumerate(shown, 1):
        res = "CONTRADICTED" + (" (hard)" if e.hard_block else f" (+{e.weight})") if e.status == "contradicted" \
            else (f"unchecked ({e.unchecked_reason})" if e.status == "unchecked" else "verified")
        out.append(f"| {i} | {e.family} | {e.check} | {e.claim} | {res} | {_cell(e.detail)} | `{e.source}` |")
    if len(shown) < len(rows):
        out.append(f"\n_{len(rows) - len(shown)} more rows in the full report._")
    out += ["", "## Finished work"]
    out += [f"- **{a.title}**" + (f" → `{a.path}`" if a.path else "") for a in verdict.actions] or ["- none"]
    out += ["", "## Safe next step", _next_step(verdict, ctx), "",
            f"Generated {ctx.get('received_at')} · {verdict.crustdata_calls} live Crustdata calls "
            f"({ctx.get('cache_hits', 0)} from cache) · ≈{verdict.crustdata_credits:.1f} credits"]
    text = "\n".join(out)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n\n_(truncated; see the report file)_"


def _cell(s: str) -> str:
    return s.replace("|", "/").replace("\n", " ")


def _next_step(verdict: Verdict, ctx) -> str:
    cb = ctx.get("callback") or {}
    contact = " / ".join(p for p in (cb.get("phone"), cb.get("email")) if p) or cb.get("website") or "the official website"
    if verdict.decision == "PAY":
        return f"Release the payment with `pay_invoice('{verdict.case_id}')`."
    if verdict.decision == "HOLD":
        return (f"Do not pay yet. Call the vendor on {contact} (found independently, not from the email), then "
                f"`resolve_case('{verdict.case_id}', 'approve'|'reject', reviewer, note)`.")
    return f"Do not pay. Warn the real vendor via {contact}; the case file is ready to report."


def save(verdict: Verdict, inv, ctx, markdown: str) -> Path:
    d = config.reports_dir() / verdict.case_id
    d.mkdir(parents=True, exist_ok=True)
    for a in verdict.actions:
        fname = re.sub(r"[^a-z_]", "", a.kind) + ".md"
        (d / fname).write_text(a.body, encoding="utf-8")
        a.path = f"reports/{verdict.case_id}/{fname}"
    (d / "report.md").write_text(render(verdict, inv, ctx, full=True), encoding="utf-8")
    data = asdict(verdict)
    data["invoice"] = {k: v for k, v in asdict(inv).items()
                       if k not in ("raw_text", "bank_iban", "bank_account")}   # edge cases Y1/Y2
    (d / "report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return d
