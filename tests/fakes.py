"""Fake checks with the spec-7 contracts, so Bahul's pipeline can be tested end to end before A and B merge.

They mimic recorded Crustdata facts (spec 6.2) and the graph rules (spec 8). They are NOT the real checks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from payeeguard.models import Evidence, Invoice

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = {
    "freshworks": {"id": 6042422, "basic_info": {"name": "Freshworks", "primary_domain": "freshworks.com",
                                                 "all_domains": ["freshworks.com"]},
                   "locations": {"all_office_addresses": ["San Mateo, CA, US", "Hatton Garden, London, EC1N 8JS, GB",
                                                          "Chennai, IN", "Paris, FR", "Berlin, DE"]},
                   "callback": {"phone": "+1 (855) 747 6767", "email": "support@freshworks.com",
                                "source": "crustdata:company/6042422#funding.phone_number"}},
    "zoho": {"id": 1032449, "basic_info": {"name": "Zoho", "primary_domain": "zoho.com",
                                           "all_domains": ["zoho.to", "zoho.com.cn", "zoho.com"]},
             "locations": {"all_office_addresses": ["Chennai, IN", "Pleasanton, CA, US"]},
             "callback": None},
}
FREE = {"gmail.com", "outlook.com", "yahoo.com"}


def api_down() -> bool:
    return os.environ.get("PAYEEGUARD_CACHE_DIR") == ".cache_empty" or os.environ.get("FAKE_API_DOWN") == "1"


def _api_error(check: str, family: str, claim: str) -> list[Evidence]:
    return [Evidence(check, family, claim, "unchecked", "Crustdata unavailable", "crustdata:api", unchecked_reason="api_error")]


def _company_for(inv: Invoice):
    key = " ".join(filter(None, [inv.vendor_domain, inv.vendor_name])).lower()
    for name, c in COMPANIES.items():
        if name in key:
            return c
    return None


def check_c1_vendor(inv: Invoice, ctx: dict, client, graph) -> list[Evidence]:
    if api_down():
        return _api_error("C1_vendor", "COMPANY", "Vendor exists")
    c = _company_for(inv)
    if not c:
        return [Evidence("C1_vendor", "COMPANY", "Vendor exists", "unchecked",
                         f"no Crustdata match for {inv.vendor_name}", f"crustdata:company/identify?names={inv.vendor_name} -> no match",
                         unchecked_reason="no_data")]
    ctx["company_id"], ctx["company"] = c["id"], c
    if c["callback"]:
        ctx["callback"] = c["callback"]
    return [Evidence("C1_vendor", "COMPANY", "Vendor exists", "verified", f"resolved {c['basic_info']['name']} (confidence 1.0)",
                     f"crustdata:company/{c['id']}#basic_info.name", confirms="who")]


def _lookalike(dom: str) -> bool:
    label = dom.split(".")[0]
    return any(b in label.replace("0", "o") and dom not in c["basic_info"]["all_domains"]
               for b, c in COMPANIES.items())


def check_c2_domain(inv: Invoice, ctx: dict, client, graph) -> list[Evidence]:
    if not inv.sender_email:
        return [Evidence("C2_domain", "CHANNEL", "Sender domain belongs to vendor", "unchecked", "no sender", "extract",
                         unchecked_reason="not_applicable")]
    dom = inv.sender_email.split("@")[1]
    if dom in FREE:
        return [Evidence("C2b_freemail", "CHANNEL", "Sender uses a company mailbox", "contradicted",
                         f"sender uses free mail ({dom})", f"policy:channel.free_mail#{dom}", weight=25)]
    if _lookalike(dom):
        return [Evidence("C2_domain", "CHANNEL", "Sender domain belongs to vendor", "contradicted",
                         f"{dom} is a lookalike of a known vendor domain", f"dns:{dom}", hard_block=True)]
    c = ctx.get("company")
    if api_down() or not c:
        return [Evidence("C2_domain", "CHANNEL", "Sender domain belongs to vendor", "unchecked", "vendor not resolved",
                         "crustdata:api", unchecked_reason="api_error" if api_down() else "no_data")]
    if dom in c["basic_info"]["all_domains"]:
        return [Evidence("C2_domain", "CHANNEL", "Sender domain belongs to vendor", "verified",
                         f"{dom} ∈ all_domains", f"crustdata:company/{c['id']}#basic_info.all_domains", confirms="channel")]
    return [Evidence("C2_domain", "CHANNEL", "Sender domain belongs to vendor", "contradicted", f"{dom} unrelated",
                     f"crustdata:company/{c['id']}#basic_info.all_domains", weight=15)]


def check_c3_signatory(inv: Invoice, ctx: dict, client, graph) -> list[Evidence]:
    return [Evidence("C3_signatory", "SIGNATORY", "Sender works at the vendor", "unchecked", "no matching person found",
                     "crustdata:person/search -> total_count=0", unchecked_reason="api_error" if api_down() else "no_data")]


def check_c5_geo(inv: Invoice, ctx: dict, client, graph) -> list[Evidence]:
    c = ctx.get("company")
    if not c or not inv.remit_country:
        return []
    countries = {o.rsplit(",", 1)[-1].strip() for o in c["locations"]["all_office_addresses"]}
    src = f"crustdata:company/{c['id']}#locations.all_office_addresses"
    if inv.remit_country in countries:
        first_time = not graph.accounts_paid(f"company:{c['id']}")
        return [Evidence("C5_geo", "PAYMENT", "Remit country is a vendor office country", "verified",
                         f"{inv.remit_country} office present", src, confirms="money" if first_time else None)]
    return [Evidence("C5_geo", "PAYMENT", "Remit country is a vendor office country", "contradicted",
                     f"no {inv.remit_country} office", src, weight=20)]


def _seed_invoices() -> set[str]:
    seed = json.loads((ROOT / "samples" / "seed_history.json").read_text(encoding="utf-8"))
    return {p["invoice_number"] for v in seed["vendors"] for p in v["payments"]}


def check_g_payment(inv: Invoice, ctx: dict, client, graph) -> list[Evidence]:
    out: list[Evidence] = []
    acct = graph.account_node(inv)
    if not acct:
        return out
    flagged = graph.cases_flagged(acct, exclude_case=ctx["case_id"])
    if flagged:
        out.append(Evidence("G3_account_flagged", "PAYMENT", "Account not linked to fraud", "contradicted",
                            f"account appears in blocked case {flagged[0]}",
                            f"graph:FLAGGED_IN/{acct}->case:{flagged[0]}", hard_block=True))
    if ctx.get("company_id"):
        comp = f"company:{ctx['company_id']}"
        paid = graph.accounts_paid(comp, exclude_case=ctx["case_id"])
        if paid and acct in paid:
            out.append(Evidence("G1_account_history", "PAYMENT", "Account matches vendor history", "verified",
                                f"{paid[acct]} prior payments to this account", f"graph:PAID/{comp}->{acct}", confirms="money"))
        elif paid:
            n = sum(paid.values())
            out.append(Evidence("G1_account_history", "PAYMENT", "Account matches vendor history", "contradicted",
                                f"{n} prior payments, never to this account", f"graph:PAID/{comp}->*", weight=25))
        if inv.invoice_number in _seed_invoices():
            out.append(Evidence("G4_duplicate", "PAYMENT", "Invoice not already paid", "contradicted",
                                f"invoice {inv.invoice_number} already PAID", f"graph:PAID/{comp}->{acct}", weight=40))
    return out


def check_c14_tactics(inv: Invoice, ctx: dict, client, graph) -> list[Evidence]:
    body = ctx.get("body", "").lower()
    if "server is down" in body or "personal email" in body:
        return [Evidence("C14_channel_switch", "BEHAVIOUR", "No pressure tactics", "contradicted",
                         "channel_switch: 'personal email'", "extract:regex#tactics", weight=5)]
    return []


FAKE_CHECKS = [
    ("check_c1_vendor", "COMPANY", check_c1_vendor),
    ("check_c2_domain", "CHANNEL", check_c2_domain),
    ("check_c3_signatory", "SIGNATORY", check_c3_signatory),
    ("check_c5_geo", "PAYMENT", check_c5_geo),
    ("check_g_payment", "PAYMENT", check_g_payment),
    ("check_c14_tactics", "BEHAVIOUR", check_c14_tactics),
]


def sample(name: str) -> str:
    return (ROOT / "samples" / name).read_text(encoding="utf-8")


def onboard_facts(vendor_name, website, client, claimed_country=None, claimed_headcount=None, claimed_offices=None):
    """Fake Mode 2 facts (Yaksh's onboard_facts contract: (list[Evidence], facts dict))."""
    from payeeguard.pipeline import website_domain
    dom = website_domain(website)
    if _lookalike(dom):
        return [Evidence("O1_website", "CHANNEL", "Website is the company's domain", "contradicted",
                         f"{dom} is a lookalike of a known company domain", f"dns:{dom}", hard_block=True)], {}
    c = next((c for c in COMPANIES.values() if dom in c["basic_info"]["all_domains"]), None)
    if not c:
        return [Evidence("C1_vendor", "COMPANY", "Vendor exists", "unchecked", f"no match for {dom}",
                         f"crustdata:company/identify?domains={dom} -> no match", unchecked_reason="no_data")], {}
    ev = [Evidence("C1_vendor", "COMPANY", "Vendor exists", "verified", "resolved", f"crustdata:company/{c['id']}",
                   confirms="who")]
    facts = {"company_id": c["id"], "name": c["basic_info"]["name"], "year_founded": 2010, "headcount": 7859,
             "leaders": [{"name": "Invented Founder", "title": "CEO", "start_date": "2010-10", "person_id": 1}],
             "phone": (c["callback"] or {}).get("phone")}
    return ev, facts
