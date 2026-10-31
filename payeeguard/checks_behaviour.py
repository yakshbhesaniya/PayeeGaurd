"""C11 bank-detail format and C14 social-engineering tactics."""

from . import config, geo
from .evidence import ev, unchecked

TACTIC_WEIGHTS = {"secrecy": 5, "channel_switch": 5, "authority": 5, "threat": 5, "payment_method": 25}


def check_c11(inv, ctx):
    claim = "Bank details are well-formed and consistent"
    meta = ctx.get("meta")
    out = []
    if not (inv.bank_iban or inv.bank_swift or inv.bank_account):
        return [unchecked("C11_bank_format", "PAYMENT", claim, "No bank details in the request", "extract",
                          "not_applicable")]
    if inv.bank_iban and not geo.iban_valid(inv.bank_iban):
        out.append(ev("C11_bank_format", "PAYMENT", claim, "contradicted",
                      f"IBAN {inv.bank_iban[:2]}••••{inv.bank_iban[-4:]} fails its checksum: typo or fabricated",
                      "extract:regex#bank_iban (ISO 13616 mod-97)", 25))
    code = inv.bank_swift
    if code and not geo.is_ifsc(code) and not geo.swift_valid(code):
        out.append(ev("C11_bank_format", "PAYMENT", claim, "contradicted", f"SWIFT/BIC {code} is malformed",
                      "extract:regex#bank_swift", 15))
    stated = meta.stated_country if meta else None
    code_country = (inv.bank_iban[:2] if inv.bank_iban else None) or \
        ("IN" if geo.is_ifsc(code) else geo.swift_country(code))
    if stated and code_country and stated != code_country:
        out.append(ev("C11_bank_format", "PAYMENT", claim, "contradicted",
                      f"Text says the bank is in {stated} but the bank code is from {code_country}",
                      "extract:regex#bank_swift", 15))
    if out:
        return [max(out, key=lambda e: e.weight)]
    return [ev("C11_bank_format", "PAYMENT", claim, "verified",
               f"Bank identifiers valid ({'IBAN checksum OK, ' if inv.bank_iban else ''}country {code_country or '?'})",
               "extract:regex#bank_iban,bank_swift")]


def check_c14(inv, ctx):
    """Quote every pressure tactic we find; BEHAVIOUR sums inside the family (capped at 25 in scoring)."""
    text = (inv.raw_text or "").lower()
    tactics = config.policy()["tactics"]
    out = []
    for tactic, weight in TACTIC_WEIGHTS.items():
        hits = [p for p in tactics.get(tactic, []) if p in text]
        if hits:
            out.append(ev(f"C14_{tactic}", "BEHAVIOUR", f"No '{tactic}' pressure tactic", "contradicted",
                          f"Quoted: \"{hits[0]}\"", f"extract:text#{tactic}", weight))
    if inv.urgency_language:
        hit = next((p for p in tactics.get("urgency", []) if p in text), "urgent wording")
        out.append(ev("C14_urgency", "BEHAVIOUR", "No urgency pressure", "contradicted", f"Quoted: \"{hit}\"",
                      "extract:text#urgency", 5))
    return out
