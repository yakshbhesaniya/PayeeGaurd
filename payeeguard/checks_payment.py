"""PAYMENT family from the graph: G1 history, G2 other vendor, G3 flagged, G4 duplicate, G5 ambiguous, C9 amount."""

from datetime import date, datetime

from . import config
from .evidence import ev, unchecked


def _cite(rel, src, dst):
    return f"graph:{rel}/{src}->{dst}"


def check_payment(inv, ctx):
    g, comp, case = ctx["graph"], ctx.get("company_node"), ctx["case_id"]
    fp, meta, known = ctx.get("fp"), ctx.get("meta"), ctx.get("vendor_known")
    pol = config.policy()["payment"]
    out = []

    if meta and len(meta.accounts) > 1:
        out.append(ev("G5_ambiguous_payment", "PAYMENT", "One unambiguous payment destination", "contradicted",
                      f"{len(meta.accounts)} different bank accounts in one email", "extract:regex#bank", 25))

    # G1: does this vendor have history with this account?
    claim = "Account matches this vendor's payment history"
    if fp is None:
        # "No account given" means "pay the account on file" only when no bank change is being requested.
        if known and not inv.bank_change_requested and (meta and (meta.on_file or not meta.accounts)):
            out.append(ev("G1_account_history", "PAYMENT", claim, "verified",
                          "No new account given: pay to the account on file", _cite("PAID", comp, "*"), confirms="money"))
        else:
            out.append(unchecked("G1_account_history", "PAYMENT", claim, "No bank account in the request", "extract",
                                 "not_applicable" if not known else "no_data"))
    elif not known:
        out.append(unchecked("G1_account_history", "PAYMENT", claim, "First payment to this vendor: no history yet",
                             _cite("PAID", comp or "?", "*"), "not_applicable"))
    else:
        paid = g.accounts_paid(comp, case)
        hit = paid.get(fp.id) or next((p for p in paid.values() if fp.weak and p["weak"] == fp.weak), None)
        total = sum(p["n"] for p in paid.values())
        if hit:
            out.append(ev("G1_account_history", "PAYMENT", claim, "verified",
                          f"Account {fp.masked} received {hit['n']} of {total} prior payments",
                          _cite("PAID", comp, hit["id"]), confirms="money"))
        else:
            known_accts = ", ".join(f"••••{p['last4']} {p['country'] or ''}".strip() for p in paid.values())
            out.append(ev("G1_account_history", "PAYMENT", claim, "contradicted",
                          f"{total} prior payments, all to {known_accts}; never to {fp.masked}",
                          _cite("PAID", comp, "*"), 25))

    if fp is not None and fp.strong:
        # G2: account paid to a different, approved vendor (factoring? aggregator? mule?)
        others = g.companies_paid(fp.id, case) - {comp}
        if others and fp.id not in pol["known_shared_accounts"]:
            out.append(ev("G2_account_other_vendor", "PAYMENT", "Account belongs only to this vendor", "contradicted",
                          f"{fp.masked} is on file for another vendor ({', '.join(sorted(others))}): "
                          "factoring company, aggregator or mule account", _cite("PAID", sorted(others)[0], fp.id), 30))
        # G3: account tied to a blocked case
        flagged = g.cases_flagged(fp.id, case)
        if flagged:
            f = flagged[0]
            out.append(ev("G3_account_flagged", "PAYMENT", "Account is not linked to a known attack", "contradicted",
                          f"{fp.masked} was used in blocked case {f['case_id']} ({_case_label(f)})",
                          _cite("FLAGGED_IN", fp.id, f"case:{f['case_id']}"), hard=True))

    # G4: duplicate / replayed invoice
    if comp and inv.invoice_number:
        row = g.invoice_paid(comp, inv.invoice_number, case)
        if row:
            same = fp is None or row["account"] == fp.id or (fp.weak and row["weak"] == fp.weak)
            detail = f"{inv.invoice_number} was already paid on {row['paid_on']}"
            if same:
                out.append(ev("G4_duplicate", "PAYMENT", "Invoice has not been paid before", "contradicted",
                              detail + " to the same account", _cite("PAID", comp, row["account"] or "*"), 40))
            else:
                out.append(ev("G4_duplicate", "PAYMENT", "Invoice has not been paid before", "contradicted",
                              detail + f" to a different account; now asked to pay {fp.masked}",
                              _cite("PAID", comp, row["account"] or "*"), hard=True))
    elif comp and inv.amount:
        window = pol["duplicate_window_days"]
        for r in g.payments_for(comp, case):
            try:
                age = (date.today() - datetime.fromisoformat(r["paid_on"]).date()).days
            except (TypeError, ValueError):
                continue
            if r["currency"] == inv.currency and abs(r["amount"] - inv.amount) <= 0.01 * inv.amount and age <= window:
                out.append(ev("G4_duplicate", "PAYMENT", "Invoice has not been paid before", "contradicted",
                              f"Same amount paid {age} days ago ({r['paid_on']})", _cite("PAID", comp, "*"), 15))
                break

    # C9: amount anomaly vs history
    if comp and inv.amount and inv.currency and known:
        med, n = g.median_amount(comp, inv.currency, case)
        if med and n >= 3:
            if inv.amount > pol["amount_anomaly_multiple"] * med:
                out.append(ev("C9_amount", "PAYMENT", "Amount is in line with history", "contradicted",
                              f"{inv.currency} {inv.amount:,.2f} is {inv.amount / med:.1f}x the median of {n} payments",
                              _cite("PAID", comp, "*"), 10))
            else:
                out.append(ev("C9_amount", "PAYMENT", "Amount is in line with history", "verified",
                              f"{inv.currency} {inv.amount:,.2f} vs median {med:,.2f} over {n} payments",
                              _cite("PAID", comp, "*")))
    return out


def _case_label(row) -> str:
    import json
    try:
        d = json.loads(row["data"] or "{}")
        return f"{d.get('attack', 'blocked')} claiming {d.get('vendor', '?')}, {row['observed_at'][:16]}"
    except (TypeError, ValueError):
        return row["observed_at"][:16]
