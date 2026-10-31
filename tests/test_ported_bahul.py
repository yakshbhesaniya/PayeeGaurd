"""Scenarios ported from Bahul's suites (test_extract, test_fuzz, test_gate, test_investigator, test_runtime,
test_samples, test_scoring, test_cli), re-expressed against the integrated API and the offline conftest harness.

Bahul's originals ran his own `analyse()` with fake checks; here the real checks run against the mock Crustdata
transport (conftest) or, for sample replays, against the committed .cache with PAYEEGUARD_OFFLINE=1 on a graph
seeded from samples/seed_history.json (exactly what `cli.py eval` does).
Tests marked xfail(strict=True, reason="BUG: ...") document real defects in the integrated code.
"""

import json
import os
import random
import subprocess
import sys
import time

import pytest

from conftest import email, seed_freshworks
from payeeguard import config, gate
from payeeguard.client import CrustdataClient
from payeeguard.extract import case_id_for, cross_check, extract, normalise_email
from payeeguard.graph import Graph, seed
from payeeguard.investigator import investigate
from payeeguard.models import Evidence
from payeeguard.pipeline import verify
from payeeguard.scoring import family_risk, score

ROOT = config.ROOT
MULE = "GB82 WEST 1234 5698 7654 32"
HERO = f"IBAN {MULE} SWIFT WESTGB2L"
ON_FILE = "Pay to account on file: Chase SWIFT CHASUS33 account 000123456789"
EXPECTED = {k: v for k, v in json.loads((ROOT / "samples" / "expected.json").read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


def checks(v):
    return {e.check: e for e in v.evidence}


def sample(name):
    return (ROOT / "samples" / name).read_text(encoding="utf-8")


@pytest.fixture
def outbox(monkeypatch):
    """Capture gate payments instead of writing the repo's payments/outbox.jsonl."""
    rows = []
    monkeypatch.setattr(gate, "_outbox", rows.append)
    return rows


@pytest.fixture
def cached(env, monkeypatch):
    monkeypatch.setenv("PAYEEGUARD_OFFLINE", "1")
    monkeypatch.setenv("PAYEEGUARD_CACHE_DIR", str(ROOT / ".cache"))
    return env


@pytest.fixture
def sg(cached):
    """(client, graph) replaying the committed Crustdata cache offline, graph seeded like `cli.py eval`."""
    c = CrustdataClient(api_key="test-key")
    g = Graph(cached / "eval_graph.db")
    seed(g, c)
    yield c, g
    g.close()


def run(sg, name_or_text, save=False, llm=None, extras=None):
    c, g = sg
    text = sample(name_or_text) if name_or_text.endswith(".txt") else name_or_text
    return verify(text, llm, client=c, graph=g, save=save, extras=extras)[0]


def run_all_samples(sg, monkeypatch, save=False):
    out = {}
    for name in sorted(EXPECTED):
        with monkeypatch.context() as m:
            for k, val in EXPECTED[name].get("env", {}).items():
                m.setenv(k, str(ROOT / val) if k == "PAYEEGUARD_CACHE_DIR" else val)
            c = CrustdataClient(api_key="test-key")
            out[name] = verify(sample(name), client=c, graph=sg[1], save=save)[0]
    return out


# ============================================================== test_extract.py

def test_forwarded_sender_from_bahul():
    assert extract(sample("02_lookalike.txt"))[0].sender_email == "rohan.verma@freshw0rks-billing.com"


@pytest.mark.parametrize("marker", ["---------- Forwarded message ---------", "Begin forwarded message:",
                                    "-----Original Message-----"])
def test_forwarded_markers_from_bahul(marker):
    text = f"From: me@buyer.com\nSubject: fwd\n\nplease check\n{marker}\nFrom: X <x@evil.com>\nPay USD 10.00"
    assert extract(text)[0].sender_email == "x@evil.com"


def test_buyer_domain_top_from_is_forward_from_bahul(monkeypatch):
    monkeypatch.setitem(config.policy()["buyer"], "buyer_domain", "buyer.com")
    text = "From: ap@buyer.com\nSubject: see below\n\nFrom: V <v@vendor.com>\nInvoice INV-1 USD 10.00"
    inv, meta = extract(text)
    assert meta.forwarded and inv.sender_email == "v@vendor.com"


def test_quoted_history_ignored_from_bahul():
    text = ("From: a@vendor.com\n\nInvoice INV-9 for USD 100.00.\n> earlier: pay USD 99,999.00 to IBAN "
            "GB33 BUKB 2020 1555 5555 55 urgent\nOn Mon, Bob wrote:\nold text USD 5,000.00 asap")
    inv, _ = extract(text)
    assert inv.amount == 100.0 and inv.bank_iban is None and not inv.urgency_language


HTML_MAIL = ("<html><body><p>From: Billing &lt;billing@freshworks.com&gt;</p><p>Invoice INV-7 total "
             "<b>USD&nbsp;4,200.00</b></p></body></html>")


def test_html_input_from_bahul():
    inv, _ = extract(HTML_MAIL)
    assert inv.amount == 4200.0 and inv.currency == "USD"


def test_html_input_sender_from_bahul():
    assert extract(HTML_MAIL)[0].sender_email == "billing@freshworks.com"


def test_amount_formats_from_bahul():
    """E6 format not already in test_edge_cases.test_E6_amount_formats."""
    inv, _ = extract("From: a@b.com\n\nPlease pay GBP 1,250.50 for invoice INV-1.")
    assert (inv.amount, inv.currency) == (1250.5, "GBP")


def test_amount_formats_single_digit_crore_from_bahul():
    inv, _ = extract("From: a@b.com\n\nPlease pay INR 2 crore for invoice INV-1.")
    assert (inv.amount, inv.currency) == (2e7, "INR")


def test_dollar_assumed_usd_note_from_bahul(client, graph):
    seed_freshworks(graph)
    v, md = verify(email(body="Pay to account on file.").replace("USD 4,200.00", "$4,200"), client=client,
                   graph=graph, save=False)
    assert "assumed" in md.lower() or any("assumed" in e.detail.lower() for e in v.evidence)


@pytest.mark.parametrize("change", [
    lambda r: r.replace("Please pay invoice INV-2026-1043 for USD 4,200.00.", "Credit note CN-4 for USD 200.00."),
    lambda r: r.replace("USD 4,200.00", "USD 0.00"),
], ids=["credit_note", "zero"])
def test_amount_missing_holds_from_bahul(client, graph, change):
    """E7: credit note / zero never PAY even when who, channel and money are all verified."""
    seed_freshworks(graph)
    v, _ = verify(change(email(body=ON_FILE)), client=client, graph=graph, save=False)
    assert v.decision == "HOLD" and v.decision_rule.startswith("R3")


def test_amount_missing_holds_negative_from_bahul(client, graph):
    seed_freshworks(graph)
    v, _ = verify(email(body=ON_FILE).replace("USD 4,200.00", "-USD 300.00"), client=client, graph=graph, save=False)
    assert v.decision == "HOLD"


def test_amount_conflict_subject_vs_body_from_bahul(client, graph):
    raw = "From: a@b.com\nSubject: Invoice INV-1 total USD 4,200.00\n\nTotal: USD 4,800.00"
    v, _ = verify(raw, client=client, graph=graph, save=False)
    x2 = checks(v)["X2_amount_conflict"]
    assert x2.status == "contradicted" and x2.weight == 10 and x2.family == "INTEGRITY"


def test_total_preferred_over_line_items_from_bahul():
    inv, _ = extract("From: a@b.com\n\nLicence USD 1,000.00\nSupport USD 200.00\nTotal due: USD 1,200.00")
    assert inv.amount == 1200.0


def test_unknown_currency_from_bahul(client, graph):
    """E9: no currency -> never PAY."""
    seed_freshworks(graph)
    raw = email(body=ON_FILE).replace("for USD 4,200.00", "amount 4200")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert v.decision == "HOLD"


def test_multiple_accounts_iban_plus_account_from_bahul(client, graph):
    raw = email(body="Pay to IBAN GB33 BUKB 2020 1555 5555 55 or account 12345678901")
    _, meta = extract(raw)
    assert len(meta.accounts) == 2
    v, _ = verify(raw, client=client, graph=graph, save=False)
    g5 = checks(v)["G5_ambiguous_payment"]
    assert g5.weight == 25 and v.decision == "HOLD"


def test_same_account_twice_not_ambiguous_from_bahul():
    _, meta = extract("From: a@b.com\n\nIBAN GB33BUKB20201555555555. Again: IBAN gb33 bukb 2020 1555 5555 55. USD 1.00")
    assert len(meta.accounts) == 1


def test_bank_normalisation_from_bahul():
    inv, _ = extract("From: a@b.com\n\nIBAN: gb33 bukb 2020 1555 5555 55 SWIFT: bukbgb22 USD 1.00")
    assert inv.bank_iban == "GB33BUKB20201555555555" and inv.bank_swift == "BUKBGB22"
    inv2, _ = extract("From: a@b.com\n\nAccount number: 12-3456-7890 USD 1.00")
    assert inv2.bank_account == "1234567890" and inv2.bank_account_last4 == "7890"


def test_iban_does_not_swallow_next_word_from_bahul():
    inv, _ = extract("From: a@b.com\n\nIBAN: DE89 3704 0044 0532 0130 00 SWIFT: COBADEFFXXX EUR 5,00")
    assert inv.bank_iban == "DE89370400440532013000"


@pytest.mark.parametrize("raw,want", [
    ("billing [at] freshworks.com", "billing@freshworks.com"),
    ("mailto:Billing@Freshworks.com", "billing@freshworks.com"),
    ("billing+inv@freshworks.com", "billing@freshworks.com"),
    ("billing@freshworks.com.", "billing@freshworks.com"),
    ("BILLING@FRESHWORKS.COM", "billing@freshworks.com"),
])
def test_email_normalisation_from_bahul(raw, want):
    assert normalise_email(raw) == want


def test_hinglish_tactics_from_bahul():
    inv, _ = extract("From: a@b.com\n\nBhai turant payment karo, abhi pay karo. Invoice INV-1 Rs. 5,000/-")
    assert inv.urgency_language and inv.amount == 5000.0 and inv.currency == "INR"


def test_ocr_noise_amount_from_bahul():
    inv, _ = extract("From: a@b.com\n\nInv0ice INV-77 T0tal: USD 4,200.00 IBAN: GB33 BUKB 2020 1555 5555 55")
    assert inv.amount == 4200.0 and inv.bank_iban == "GB33BUKB20201555555555"


def test_huge_input_from_bahul():
    """E15: extraction reads only the first 20k chars; case_id hashes the full text."""
    big = "From: a@b.com\n\nInvoice INV-1 for USD 10.00\n" + "x" * 50_000 + "\nIBAN GB33 BUKB 2020 1555 5555 55"
    inv, _ = extract(big)
    assert inv.bank_iban is None and inv.amount == 10.0
    assert case_id_for(big) != case_id_for(big[:20_000])


def test_field_truncation_from_bahul():
    inv, _ = extract(sample("01_legit.txt"))
    inv, _ = cross_check(inv, {"vendor_name": "A" * 5000, "sender_title": "B" * 5000})
    assert len(inv.sender_title) == 200
    assert inv.vendor_name == "Freshworks Inc"                     # regex value kept


def test_server_x1_mismatch_from_bahul(sg):
    """M2 / sample 08: injected sender is ignored; X1 shows both values; regex wins; BLOCK."""
    v = run(sg, "08_prompt_injection.txt", llm={"sender_email": "billing@freshworks.com"})
    x1 = checks(v)["X1_extraction"]
    assert x1.weight == 10 and "billing@freshworks.com" in x1.detail and "accounts@freshworks-invoices.com" in x1.detail
    assert v.decision == "BLOCK"


def test_x1_value_not_in_text_ignored_from_bahul():
    inv, _ = extract("From: a@b.com\n\nInvoice INV-1 USD 10.00")
    inv, rows = cross_check(inv, {"bank_iban": "GB33BUKB20201555555555"})
    assert inv.bank_iban is None and rows and rows[0].check == "X1_extraction"
    assert "5555" in rows[0].detail and "GB33BUKB" not in rows[0].detail


def test_x1_sender_injection_without_from_header_from_bahul(client, graph):
    """M2 / H11: with no From header, a Claude-supplied sender that isn't in the text is ignored (X1), so the
    channel stays unverified. (A known vendor paying to the account on file may still PAY by R6 at MEDIUM:
    that is spec 10.4 behaviour, so the decision itself is not asserted.)"""
    seed_freshworks(graph)
    raw = ("Subject: Invoice INV-2026-1043\n\nPlease pay invoice INV-2026-1043 for USD 4,200.00 to our account on "
           "file.\nVendor: Freshworks Inc, freshworks.com")
    v, _ = verify(raw, {"sender_email": "billing@freshworks.com"}, client=client, graph=graph, save=False)
    assert checks(v)["X1_extraction"].status == "contradicted"
    assert checks(v)["C2_domain"].status != "verified" and v.confidence != "HIGH"


def test_x1_fills_gap_when_value_is_in_text_from_bahul():
    inv, _ = extract("Please pay invoice INV-1 USD 10.00 from billing@vendor.com")
    # Grounding rule (integrated design): identity fields from Claude must appear in the email text.
    inv, rows = cross_check(inv, {"vendor_name": "Vendor Ltd", "bank_change_requested": True})
    assert inv.vendor_name is None and inv.bank_change_requested and rows      # ungrounded name ignored + reported
    inv2, _ = extract("Please pay invoice INV-1 USD 10.00 from Vendor Ltd, billing@vendor.com")
    inv2.vendor_name = None
    inv2, rows2 = cross_check(inv2, {"vendor_name": "Vendor Ltd"})
    assert inv2.vendor_name == "Vendor Ltd" and not rows2                      # grounded name accepted


def test_llm_cannot_clear_caution_flags_from_bahul():
    inv, _ = extract(sample("05_zoho_lookalike_mule.txt"))
    inv, _ = cross_check(inv, {"bank_change_requested": False, "urgency_language": False})
    assert inv.bank_change_requested


@pytest.mark.parametrize("name,country", [("01_legit.txt", "US"), ("02_lookalike.txt", "NG"),
                                          ("03_gmail_signatory.txt", "IN"), ("05_zoho_lookalike_mule.txt", "GB"),
                                          ("07_unknown_vendor.txt", "IN"), ("08_prompt_injection.txt", "DE")])
def test_remit_country_from_bahul(name, country):
    assert extract(sample(name))[0].remit_country == country


def test_injected_vendor_domain_ignored_from_bahul(sg):
    v = run(sg, "05_zoho_lookalike_mule.txt", llm={"vendor_domain": "freshworks.com"})
    assert v.decision == "BLOCK"


def test_non_iban_codes_not_taken_as_iban_from_bahul():
    inv, _ = extract("From: a@b.com\n\nOrder ZZ12 3456 7890 1234 56 and ref AB12CD34EF56GH78 for USD 5.00")
    assert inv.bank_iban is None


def test_invalid_checksum_iban_still_extracted_from_bahul(client, graph):
    """C11 must see a bad IBAN to flag it, so extraction keeps it."""
    raw = email(body="Pay IBAN: GB00 WEST 1234 5698 7654 32")
    assert extract(raw)[0].bank_iban == "GB00WEST12345698765432"
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert checks(v)["C11_bank_format"].status == "contradicted"


@pytest.mark.parametrize("junk", ["a" * 20000, "GB82 " * 4000, "@" * 20000, "On " * 7000,
                                  ("x" * 50 + "\n") * 400, "From: " + "<" * 20000, "USD " + "9 " * 10000],
                         ids=["a", "GB82", "at", "On", "lines", "lt", "USD9"])
def test_regex_no_catastrophic_backtracking_from_bahul(junk):
    t = time.perf_counter()
    extract("From: a@b.com\n\n" + junk)
    assert time.perf_counter() - t < 2.0


def test_regex_no_catastrophic_backtracking_digit_comma_run_from_bahul():
    t = time.perf_counter()
    extract("From: a@b.com\n\n" + "1," * 3000)
    assert time.perf_counter() - t < 0.5


# ============================================================== test_fuzz.py

SAMPLES = [p.read_text(encoding="utf-8") for p in sorted((ROOT / "samples").glob("*.txt"))]
SECRETS = ("000123456789", "12345698765432", "GB82WEST", "GB33BUKB", "DE89370400440532013000", "123405000789",
           "50200012345678")
INJECT = ["\n> quoted USD 99,999.00", "\n<div>Total: EUR 1.000,00</div>", "\nPay to account 9876543210123",
          "\nIBAN: GB33 BUKB 2020 1555 5555 55", "\nThis is a credit note", "\nURGENT turant", "\n\x00\x01﻿",
          "\n---------- Forwarded message ---------\nFrom: x@gmail.com\n", "\nReply-To: a@zoh0.com"]


def mutate(text: str, rng: random.Random) -> str:
    lines = text.split("\n")
    for _ in range(rng.randint(1, 4)):
        op = rng.randrange(6)
        if op == 0 and len(lines) > 1:
            del lines[rng.randrange(len(lines))]
        elif op == 1 and len(lines) > 2:
            i, j = rng.randrange(len(lines)), rng.randrange(len(lines))
            lines[i], lines[j] = lines[j], lines[i]
        elif op == 2:
            i = rng.randrange(len(lines))
            lines[i] = lines[i].upper() if rng.random() < .5 else lines[i].lower()
        elif op == 3:
            lines.insert(rng.randrange(len(lines) + 1), rng.choice(INJECT))
        elif op == 4:
            cut = rng.randrange(len("\n".join(lines)) + 1)
            lines = "\n".join(lines)[:cut].split("\n")
        else:
            lines = ["<html><body>"] + [f"<p>{line}</p>" for line in lines] + ["</body></html>"]
    return "\n".join(lines)


def test_fuzz_invariants_from_bahul(client, graph):
    """Mutated samples never crash verify and never break the spec's safety rules (10.1, R3, 13.8 Y2)."""
    seed_freshworks(graph)
    rng = random.Random(1031)
    for n in range(300):
        text = mutate(rng.choice(SAMPLES), rng)
        extras: dict = {}
        v, md = verify(text, client=client, graph=graph, save=False, extras=extras)
        inv = extras.get("invoice") or {}
        ctx = f"mutation {n}: {text[:300]!r}"
        assert v.decision in ("PAY", "HOLD", "BLOCK"), ctx
        contradicted = [e for e in v.evidence if e.status == "contradicted"]
        if v.decision == "BLOCK":                                   # BLOCK needs proof (10.1 rule 5)
            assert contradicted, ctx
        if v.decision == "PAY":                                     # never auto-pay these (10.1, R3)
            assert not any(e.hard_block for e in contradicted), ctx
            assert not inv.get("bank_change") and inv.get("amount") and inv["amount"] > 0, ctx
            assert v.confidence in ("HIGH", "MEDIUM") and v.risk_score < 25, ctx
        if not contradicted:
            assert v.risk_score == 0 and v.decision != "BLOCK", ctx  # absence is never fraud
        for s in SECRETS:
            assert s not in md.replace(" ", ""), (s, ctx)


def test_fuzz_extract_never_raises_from_bahul():
    rng = random.Random(7)
    alphabet = "abcXYZ019 ,.:;@<>[]()$€£₹-/\n\t\"'"
    for _ in range(2000):
        extract("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 300))))


def test_fuzz_verify_garbled_text_never_raises_from_bahul(client, graph):
    """Random garbled text (with payment-looking fragments) through the whole pipeline never crashes."""
    rng = random.Random(11)
    alphabet = "abcXYZ019 ,.:;@<>[]()$€£₹-/\n\t\"'"
    frags = ["From: ", "Reply-To: ", "IBAN ", "SWIFT ", "USD ", "invoice ", "INV-2026-", "account ", "\n> ",
             "<p>", "&amp;", "Vendor: ", "@freshworks.com", "lakh", "/-"]
    for _ in range(150):
        parts = [rng.choice(frags) if rng.random() < .3 else rng.choice(alphabet) for _ in range(rng.randint(0, 120))]
        v, _ = verify("".join(parts), client=client, graph=graph, save=False)
        assert v.decision in ("PAY", "HOLD", "BLOCK")


# ============================================================== test_gate.py

def test_gate_unknown_case_from_bahul(graph, outbox):
    assert "Unknown case" in gate.pay_invoice("deadbeef0000", graph) and not outbox
    assert "Unknown case" in gate.pay_invoice("../../etc/passwd", graph) and not outbox


@pytest.mark.parametrize("name,status", [("04_new_account_real_domain.txt", "HOLD"), ("02_lookalike.txt", "BLOCK")])
def test_gate_refuses_from_bahul(sg, outbox, name, status):
    """P2: refuse, quoting the state, the decision rule and the top evidence."""
    v = run(sg, name)
    msg = gate.pay_invoice(v.case_id, sg[1])
    assert msg.startswith("Refused") and status in msg and v.decision_rule in msg and "Top evidence" in msg
    assert not outbox


def test_gate_pays_pay_once_from_bahul(sg, outbox):
    v = run(sg, "01_legit.txt")
    assert gate.pay_invoice(v.case_id, sg[1]).startswith("RELEASED")
    assert len(outbox) == 1 and outbox[0]["requires_bank_upload"] is True and "000123456789" not in json.dumps(outbox[0])
    assert "already paid" in gate.pay_invoice(v.case_id, sg[1])
    assert len(outbox) == 1


def test_gate_no_double_pay_after_reverify_from_bahul(sg, outbox):
    """G-3: the same text verified again keeps RELEASED and the gate still refuses."""
    v = run(sg, "01_legit.txt")
    gate.pay_invoice(v.case_id, sg[1])
    run(sg, "01_legit.txt")
    assert sg[1].get_case(v.case_id)["state"] == "RELEASED"
    assert "already paid" in gate.pay_invoice(v.case_id, sg[1]) and len(outbox) == 1


def test_resolve_pay_case_from_bahul(sg):
    """P3: resolve_case on a PAY case is not needed (integrated wording: 'nothing to resolve')."""
    v = run(sg, "01_legit.txt")
    assert "nothing to resolve" in gate.resolve_case(v.case_id, "approve", "asha", "ok", sg[1])
    assert sg[1].get_case(v.case_id)["state"] == "PAY"


def test_approval_not_transferable_from_bahul(sg, outbox):
    """P4: a new email with different bank details is a new case; the approval does not carry over."""
    v = run(sg, "04_new_account_real_domain.txt")
    gate.resolve_case(v.case_id, "approve", "asha", "called vendor", sg[1])
    v2 = run(sg, sample("04_new_account_real_domain.txt").replace("GB33 BUKB 2020 1555 5555 55", MULE))
    assert v2.case_id != v.case_id
    assert gate.pay_invoice(v2.case_id, sg[1]).startswith("Refused") and not outbox


def test_dual_approval_distinct_on_pay_from_bahul(sg, outbox):
    v = run(sg, sample("01_legit.txt").replace("USD 4,200.00", "USD 30,000.00"))
    assert v.decision == "PAY"
    assert gate.pay_invoice(v.case_id, sg[1]).startswith("Refused")
    gate.resolve_case(v.case_id, "approve", "asha", "ok", sg[1])
    assert "different reviewer" in gate.resolve_case(v.case_id, "approve", "asha", "again", sg[1])
    gate.resolve_case(v.case_id, "approve", "ravi", "second approver", sg[1])
    assert gate.pay_invoice(v.case_id, sg[1]).startswith("RELEASED")


def test_hold_reject_becomes_block_from_bahul(sg, outbox):
    v = run(sg, "04_new_account_real_domain.txt")
    assert "now BLOCK" in gate.resolve_case(v.case_id, "reject", "asha", "vendor denied", sg[1])
    case = sg[1].get_case(v.case_id)
    assert case["state"] == "BLOCK"
    assert [r["case_id"] for r in sg[1].cases_flagged(case["data"]["fp"]["id"])] == [v.case_id]
    assert gate.pay_invoice(v.case_id, sg[1]).startswith("Refused") and not outbox


def test_override_revokes_flags_then_hero_holds_from_bahul(sg):
    """G-7: override on a BLOCK needs a note, revokes its FLAGGED_IN edges, and un-links the hero campaign."""
    g = sg[1]
    v = run(sg, "05_zoho_lookalike_mule.txt")
    acct = g.get_case(v.case_id)["data"]["fp"]["id"]
    assert [r["case_id"] for r in g.cases_flagged(acct)] == [v.case_id]
    assert "required" in gate.resolve_case(v.case_id, "override", "asha", "", g)
    assert "only a HOLD" in gate.resolve_case(v.case_id, "approve", "asha", "ok", g)
    assert "overridden" in gate.resolve_case(v.case_id, "override", "asha", "vendor confirmed on call", g)
    assert g.cases_flagged(acct) == []
    hero = run(sg, "06_hero_freshworks_mule.txt")
    assert hero.decision == "HOLD"                    # no longer linked to a blocked case


def test_override_not_for_hold_from_bahul(sg):
    v = run(sg, "04_new_account_real_domain.txt")
    assert "only for a BLOCK" in gate.resolve_case(v.case_id, "override", "asha", "x", sg[1])


def test_resolve_validation_from_bahul(sg):
    v = run(sg, "04_new_account_real_domain.txt")
    assert "must be approve" in gate.resolve_case(v.case_id, "maybe", "asha", "x", sg[1])
    assert "required" in gate.resolve_case(v.case_id, "approve", "", "x", sg[1])
    assert "Unknown case" in gate.resolve_case("abc", "approve", "asha", "x", sg[1])
    assert sg[1].get_case(v.case_id)["state"] == "HOLD"


def test_resolve_validation_whitespace_from_bahul(client, graph):
    v, _ = verify(email("Bad <bad@freshw0rks-pay.com>", body=f"New bank details: {HERO}"), client=client,
                  graph=graph, save=False)
    gate.resolve_case(v.case_id, "override", "  ", "   ", graph)
    assert graph.get_case(v.case_id)["state"] == "BLOCK"


def test_case_status_from_bahul(sg):
    v = run(sg, "02_lookalike.txt")
    assert "decision BLOCK" in gate.case_status(v.case_id, sg[1])
    assert "Unknown case" in gate.case_status("nope", sg[1])


def test_case_record_has_no_raw_text_or_account_from_bahul(sg):
    """Y1 / Y2 for the stored case record."""
    v = run(sg, "06_hero_freshworks_mule.txt")
    blob = json.dumps(sg[1].get_case(v.case_id))
    assert "regional treasury" not in blob and "12345698765432" not in blob and "GB82WEST" not in blob


def test_approval_voided_by_new_block_from_bahul(sg, outbox):
    v = run(sg, "04_new_account_real_domain.txt")
    gate.resolve_case(v.case_id, "approve", "asha", "called vendor", sg[1])
    attack = sample("05_zoho_lookalike_mule.txt").replace(MULE, "GB33 BUKB 2020 1555 5555 55")
    assert run(sg, attack).decision == "BLOCK"
    again = run(sg, "04_new_account_real_domain.txt")
    assert again.decision == "BLOCK"
    assert gate.pay_invoice(v.case_id, sg[1]).startswith("Refused")


# ============================================================== test_investigator.py

def E(check, family, status="contradicted", weight=0, hard=False, confirms=None, reason=None, detail="d"):
    return Evidence(check, family, "claim", status, detail, "src", weight=weight, hard_block=hard,
                    unchecked_reason=reason, confirms=confirms)


C1 = E("C1_vendor", "COMPANY", "verified", confirms="who")
C2 = E("C2_domain", "CHANNEL", "verified", confirms="channel")
G1 = E("G1_account_history", "PAYMENT", "verified", confirms="money")
G1X = E("G1_account_history", "PAYMENT", weight=25, detail="14 prior payments, never to ••••5432 GB")
C5 = E("C5_geo", "PAYMENT", "verified", detail="GB office present")
C1_NODATA = E("C1_vendor", "COMPANY", "unchecked", reason="no_data")
LOOKALIKE = E("C2_lookalike", "CHANNEL", hard=True, detail="freshw0rks-billing.com is a lookalike of freshworks.com")
G3 = E("G3_account_flagged", "PAYMENT", hard=True, detail="account appears in blocked case a71f")


def attack(ev):
    h, also, ruled = investigate(ev)
    return (h.attack_type if h else "none"), [a.attack_type for a in also], {r.attack_type for r in ruled}


def test_hero_mule_plus_compromised_from_bahul():
    primary, also, ruled = attack([C1, C2, C5, G3, G1X])
    assert primary == "mule_campaign" and "compromised_vendor_mailbox" in also
    assert {"vendor_impersonation", "unverifiable_vendor", "wrong_geography"} <= ruled


def test_mule_beats_compromised_even_with_more_support_from_bahul():
    ev = [C1, C2, G3, G1X, E("C14_urgency", "BEHAVIOUR", weight=5), E("C5_geo", "PAYMENT", weight=20)]
    assert attack(ev)[0] == "mule_campaign"


def test_impersonation_from_bahul():
    assert attack([C1, LOOKALIKE])[0] == "vendor_impersonation"


def test_reply_to_lookalike_is_impersonation_from_bahul():
    assert attack([C1, C2, E("C10_reply_to_lookalike", "CHANNEL", hard=True, detail="reply-to lookalike")])[0] \
        == "vendor_impersonation"


def test_compromised_mailbox_from_bahul():
    primary, _, ruled = attack([C1, C2, G1X])
    assert primary == "compromised_vendor_mailbox" and "vendor_impersonation" in ruled


def test_duplicate_replay_from_bahul():
    assert attack([C1, C2, G1, E("G4_duplicate", "PAYMENT", weight=40, detail="INV-2026-1031 already PAID")])[0] \
        == "duplicate_replay"


def test_ex_employee_from_bahul():
    assert attack([C1, E("C3_ex_employee", "SIGNATORY", weight=35, detail="left Freshworks 2024-12"), G1X])[0] \
        == "ex_employee"


def test_unauthorized_change_from_bahul():
    assert attack([C1, C2, E("C3_role_authority", "SIGNATORY", weight=20, detail="engineer")])[0] \
        == "unauthorized_change"


def test_unverified_signatory_from_bahul():
    ev = [C1, E("C2b_freemail", "CHANNEL", weight=25, detail="free mail gmail.com"),
          E("C3_signatory", "SIGNATORY", "unchecked", reason="no_data")]
    assert attack(ev)[0] == "unverified_signatory"
    assert attack(ev[:2] + [E("C3_signatory", "SIGNATORY", "verified")])[0] != "unverified_signatory"


def test_unverifiable_vendor_only_on_no_data_from_bahul():
    assert attack([C1_NODATA])[0] == "unverifiable_vendor"
    assert attack([E("C1_vendor", "COMPANY", "unchecked", reason="api_error")])[0] == "none"


def test_payment_method_from_bahul():
    assert attack([C1, C2, G1, E("C14_payment_method", "BEHAVIOUR", weight=25, detail="USDT")])[0] \
        == "payment_method_fraud"


def test_multiple_patterns_from_bahul():
    """S7: primary by priority, the rest in also_matched."""
    primary, also, _ = attack([LOOKALIKE, G3, G1X])
    assert primary == "mule_campaign" and also == ["vendor_impersonation"]


def test_none_when_clean_from_bahul():
    h, also, _ = investigate([C1, C2, G1])
    assert h is None and not also


def test_narrative_uses_cited_details_from_bahul():
    h, _, _ = investigate([C1, C2, G3, G1X])
    assert "account appears in blocked case a71f" in h.narrative
    assert set(h.supporting) >= {"G3_account_flagged", "G1_account_history"}


def test_c3_no_data_never_ex_employee_from_bahul():
    """Absence is never fraud: an unchecked C3 can't produce a signatory attack."""
    assert attack([C1, C2, G1, E("C3_ex_employee", "SIGNATORY", "unchecked", weight=35, reason="no_data")])[0] == "none"


# ============================================================== test_runtime.py

def test_check_crash_isolated_from_bahul(client, graph, monkeypatch):
    """S10: a crashing check becomes unchecked/api_error, its message is not shown, and a legit invoice still PAYs."""
    import payeeguard.pipeline as p
    seed_freshworks(graph)

    def boom(inv, ctx):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(p, "check_c4", boom)
    v, md = verify(email(body=ON_FILE), client=client, graph=graph, save=False)
    row = checks(v)["C4_shell"]
    assert row.status == "unchecked" and row.unchecked_reason == "api_error"
    assert "secret detail" not in row.detail and "secret detail" not in md
    assert v.decision == "PAY"


@pytest.fixture
def server_mod(cached, monkeypatch):
    import server
    monkeypatch.setattr(server, "_client", CrustdataClient(api_key="test-key"))
    db = cached / "server_graph.db"
    g = Graph(db)
    seed(g, server._client)
    g.close()
    monkeypatch.setattr(server, "Graph", lambda: Graph(db))
    return server


def test_server_raw_only_from_bahul(server_mod):
    """M1: no fields, only raw_text."""
    assert server_mod.verify_payee(sample("01_legit.txt")).startswith("# PayeeGuard verdict: PAY")


def test_server_x1_through_tool_from_bahul(server_mod):
    """M2 through the MCP tool: the injected sender is overruled and shown."""
    out = server_mod.verify_payee(sample("08_prompt_injection.txt"), sender_email="billing@freshworks.com")
    assert "verdict: BLOCK" in out and "X1_extraction" in out


def test_server_tools_registered_with_schema_from_bahul():
    import asyncio
    import server
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {"verify_payee", "onboard_vendor", "pay_invoice", "resolve_case", "case_status"}
    props = tools["verify_payee"].inputSchema["properties"]
    assert "raw_text" in props and "bank_iban" in props
    assert tools["verify_payee"].inputSchema["required"] == ["raw_text"]


def test_paths_repo_relative_from_bahul(tmp_path):
    """M3: started from another directory, .env/policy resolve from the repo root."""
    code = ("import server, payeeguard.config as c; print(c.ROOT); "
            "print(c.policy()['decision']['auto_pay_limit_usd'])")
    out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, r'{ROOT}'); {code}"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert str(ROOT) in out.stdout and "5000" in out.stdout


def test_utf8_output_from_bahul(tmp_path):
    """M4: the CLI prints non-ASCII (₹, •) on a cp1252 console without crashing (offline, outputs in tmp)."""
    f = tmp_path / "in.txt"
    f.write_text("From: Opéra <billing@opera.example>\n\nInvoice INV-1 for ₹3,45,000 due", encoding="utf-8")
    code = (f"import sys; sys.path.insert(0, r'{ROOT}'); from pathlib import Path; "
            f"import payeeguard.config as c; c.reports_dir = lambda: Path(r'{tmp_path}') / 'reports'; "
            f"import cli; sys.exit(cli.main(['verify', r'{f}']))")
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PAYEEGUARD_OFFLINE="1",
               PAYEEGUARD_CACHE_DIR=str(tmp_path / "cache"), PAYEEGUARD_DATA_DIR=str(tmp_path / "data"),
               PAYEEGUARD_FP_SECRET="test-secret")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, cwd=tmp_path, timeout=60)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert "INR 345,000.00" in out.stdout.decode("utf-8")


def _many_rows(inv, ctx):
    return [Evidence(f"Z{i}_x", "CONTEXT", "c", "verified", "d" * 400, "s") for i in range(40)]


def test_report_truncation_from_bahul(client, graph, env, monkeypatch):
    """M5: chat report is capped (~15k chars, top rows only); the saved report.md has the full ledger."""
    import payeeguard.pipeline as p
    monkeypatch.setattr(p, "check_c6", _many_rows)
    v, md = verify(email(), client=client, graph=graph, save=True)
    assert len(md) <= 15_100 and "more rows in the full report" in md


def test_report_truncation_full_file_from_bahul(client, graph, env, monkeypatch):
    import payeeguard.pipeline as p
    monkeypatch.setattr(p, "check_c6", _many_rows)
    v, _ = verify(email(), client=client, graph=graph, save=True)
    full = (env / "reports" / v.case_id / "report.md").read_text(encoding="utf-8")
    assert full.count("| Z") == 40


def test_report_truncation_file_path_from_bahul(client, graph, monkeypatch):
    import payeeguard.pipeline as p
    monkeypatch.setattr(p, "check_c6", _many_rows)
    v, md = verify(email(), client=client, graph=graph, save=True)
    assert "report.md" in md


def test_cache_atomic_write_from_bahul(client, graph, env):
    """M6: cache entries are written via temp file + rename; no temp files left behind."""
    for _ in range(2):
        verify(email(), client=client, graph=graph, save=False)
    files = list((env / "cache").rglob("*"))
    assert any(f.suffix == ".json" for f in files) and not any(f.suffix == ".tmp" for f in files)


@pytest.fixture
def policy_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    config.policy.cache_clear()
    yield tmp_path
    config.policy.cache_clear()


def test_policy_defaults_missing_from_bahul(policy_root):
    """M7: missing policy.toml -> built-in defaults."""
    assert config.policy()["decision"]["block_risk"] == 60


def test_policy_defaults_invalid_from_bahul(policy_root):
    (policy_root / "policy.toml").write_text("[decision\nblock_risk = ", encoding="utf-8")
    assert config.policy()["decision"]["hold_risk"] == 25


def test_policy_defaults_partial_from_bahul(policy_root):
    (policy_root / "policy.toml").write_text("[decision]\nblock_risk = 70\n", encoding="utf-8")
    p = config.policy()
    assert p["decision"]["block_risk"] == 70 and p["decision"]["hold_risk"] == 25


def _all_outputs(env) -> str:
    out = []
    for p in env.rglob("*"):
        if p.is_file() and "cache" not in p.parts:
            out.append(p.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(out)


def test_no_raw_text_persisted_from_bahul(sg, cached):
    """Y1: reports/ and data/ never hold the email body."""
    for name in ("03_gmail_signatory.txt", "06_hero_freshworks_mule.txt", "07_unknown_vendor.txt"):
        run(sg, name, save=True)
    sg[1].commit()
    blobs = _all_outputs(cached)
    for phrase in ("Using personal email as our server is down", "regional treasury setup", "IT consulting services"):
        assert phrase not in blobs


def test_no_full_account_in_outputs_from_bahul(sg, cached, outbox, monkeypatch):
    """Y2: only country + last 4 anywhere in reports, the audit log, the graph db and the outbox."""
    for name, v in run_all_samples(sg, monkeypatch, save=True).items():
        gate.pay_invoice(v.case_id, sg[1])
    sg[1].commit()
    blobs = _all_outputs(cached) + json.dumps(outbox)
    for secret in ("000123456789", "12345698765432", "GB82WEST", "GB82 WEST", "GB33BUKB", "DE89370400440532013000",
                   "DE89 3704", "123405000789", "50200012345678"):
        assert secret not in blobs, secret


def test_artifacts_labelled_from_bahul(sg, cached):
    """Y4: every outbound artifact (PAY / HOLD / BLOCK / unknown vendor) carries DRAFT FOR HUMAN RELEASE."""
    for name in ("01_legit.txt", "04_new_account_real_domain.txt", "02_lookalike.txt", "07_unknown_vendor.txt"):
        v = run(sg, name, save=True)
        assert v.actions
        for a in v.actions:
            text = (cached / a.path.replace("reports/", "reports/", 1)).read_text(encoding="utf-8")
            assert "DRAFT FOR HUMAN RELEASE" in text, (name, a.kind)


# ============================================================== test_samples.py

def test_all_samples_hit_expected_from_bahul(sg, monkeypatch):
    got = {n: (v.decision, v.hypothesis.attack_type if v.hypothesis else "none")
           for n, v in run_all_samples(sg, monkeypatch).items()}
    assert got == {n: (e["decision"], e["attack_type"]) for n, e in EXPECTED.items()}


def test_hero_sequence_links_campaign_from_bahul(sg, cached, monkeypatch):
    vs = run_all_samples(sg, monkeypatch, save=True)
    hero, zoho = vs["06_hero_freshworks_mule.txt"], vs["05_zoho_lookalike_mule.txt"]
    assert hero.decision_rule.startswith("R1 hard fact G3")
    assert "compromised_vendor_mailbox" in [h.attack_type for h in hero.also_matched]
    assert any(zoho.case_id in e["dst_label"] for e in hero.graph_edges if e["rel"] == "FLAGGED_IN")
    ruled = {r.attack_type for r in hero.ruled_out}
    assert {"vendor_impersonation", "fake_vendor", "wrong_geography"} <= ruled            # S6
    text = (cached / "reports" / hero.case_id / "fraud_case_file.md").read_text(encoding="utf-8")
    assert zoho.case_id in text
    assert "GB82" not in text and "12345698765432" not in text                           # Y2


def test_hero_without_05_is_hold_not_block_from_bahul(sg):
    v = run(sg, "06_hero_freshworks_mule.txt")
    assert v.decision == "HOLD" and v.hypothesis.attack_type == "compromised_vendor_mailbox"


def test_deterministic_from_bahul(sg):
    """S11: identical verdict, rule and report text except the timestamp line."""
    texts = []
    for _ in range(2):
        c, g = sg
        _, md = verify(sample("04_new_account_real_domain.txt"), client=c, graph=g, save=False)
        texts.append("\n".join(line for line in md.split("\n") if not line.startswith("Generated")))
    assert texts[0] == texts[1]


def test_api_down_holds_from_bahul(sg, monkeypatch):
    """S1 / sample 10: Crustdata down -> HOLD 'could not verify', never BLOCK or PAY.
    (Bahul also required confidence LOW / rule R7; integrated keeps graph-confirmed facts and uses an explicit
    'R3 Crustdata unavailable' rule, so only the decision and the wording are asserted.)"""
    monkeypatch.setenv("PAYEEGUARD_CACHE_DIR", str(ROOT / ".cache_empty"))
    v = run((CrustdataClient(api_key="test-key"), sg[1]), "01_legit.txt")
    assert v.decision == "HOLD" and "could not verify" in v.decision_rule


def test_unknown_vendor_holds_from_bahul(sg):
    """S3 / sample 07: unknown vendor -> HOLD + KYB task, not BLOCK."""
    v = run(sg, "07_unknown_vendor.txt")
    assert v.decision == "HOLD" and "kyb_onboarding_task" in [a.kind for a in v.actions]


@pytest.mark.parametrize("name", ["02_lookalike.txt", "08_prompt_injection.txt"])
def test_lookalikes_block_from_bahul(sg, name):
    v = run(sg, name)
    assert v.decision == "BLOCK" and v.decision_rule.startswith("R1")


# ============================================================== test_scoring.py

def S(ev, amount=4200.0, currency="USD", bank_change=False, vendor_known=False, credit_note=False):
    return score(ev, vendor_known=vendor_known, amount=amount, currency=currency, bank_change=bank_change,
                 credit_note=credit_note)


ROWS = [
    # name, evidence, kwargs, risk, confidence, rule prefix, decision
    ("01 legit", [C1, C2, G1], {}, 0, "HIGH", "R5", "PAY"),
    ("02 lookalike", [C1, LOOKALIKE], {"amount": 18400}, None, None, "R1", "BLOCK"),
    ("03 gmail", [C1, E("C2b_freemail", "CHANNEL", weight=25),
                  E("C3_signatory", "SIGNATORY", "unchecked", reason="no_data"),
                  E("C14_channel_switch", "BEHAVIOUR", weight=5)],
     {"amount": 345000, "currency": "INR", "bank_change": True}, 30, None, "R3", "HOLD"),
    ("04 new account", [C1, C2, G1X, C5], {}, 25, None, "R3", "HOLD"),
    ("04 + no GB office", [C1, C2, G1X, E("C5_geo", "PAYMENT", weight=20)], {}, 25, None, "R3", "HOLD"),
    ("05 zoho lookalike", [LOOKALIKE], {"amount": 210000, "currency": "INR", "bank_change": True}, None, None, "R1",
     "BLOCK"),
    ("06 hero", [C1, C2, E("C5_geo", "PAYMENT", "verified", confirms="money"), G3, G1X], {"amount": 4350}, None, None,
     "R1", "BLOCK"),
    ("07 unknown vendor", [C1_NODATA], {"amount": 185000, "currency": "INR", "bank_change": True}, 0, "LOW", "R3",
     "HOLD"),
    ("08 injection", [LOOKALIKE, E("X1_extraction", "INTEGRITY", weight=10)], {"amount": 9800, "currency": "EUR"},
     None, None, "R1", "BLOCK"),
    ("09 duplicate", [C1, C2, G1, E("G4_duplicate", "PAYMENT", weight=40)], {}, 40, "HIGH", "R4", "HOLD"),
    ("small known vendor", [C1_NODATA, C2, G1], {"amount": 1200, "vendor_known": True}, 0, "HIGH", "R5", "PAY"),
    ("small known vendor, history channel", [C1_NODATA, G1], {"amount": 1200, "vendor_known": True}, 0, "MEDIUM", "R6",
     "PAY"),
    ("ex-employee new account", [C1, E("C3_ex_employee", "SIGNATORY", weight=35), G1X], {}, 60, None, "R2", "BLOCK"),
    ("engineer bank change", [C1, C2, E("C3_role_authority", "SIGNATORY", weight=20), G1X], {"bank_change": True}, 45,
     None, "R3", "HOLD"),
]


@pytest.mark.parametrize("name,ev,kw,risk,conf,rule,decision", ROWS, ids=[r[0] for r in ROWS])
def test_table_10_5_from_bahul(name, ev, kw, risk, conf, rule, decision):
    s = S(ev, **kw)
    assert s.decision == decision and s.rule.startswith(rule)
    if risk is not None:
        assert s.risk == risk
    if conf is not None:
        assert s.confidence == conf


def test_table_10_5_api_down_from_bahul():
    """Row '10 api down': HOLD with 'could not verify' (integrated labels it R3, spec table says R7)."""
    ev = [E(c, f, "unchecked", reason="api_error") for c, f in
          [("C1_vendor", "COMPANY"), ("C2_domain", "CHANNEL"), ("C3_signatory", "SIGNATORY")]]
    s = S(ev)
    assert s.decision == "HOLD" and s.risk == 0 and s.confidence == "LOW" and "could not verify" in s.rule


def test_no_double_count_family_split_from_bahul():
    ev = [E("C2b_freemail", "CHANNEL", weight=25), E("C2_unrelated", "CHANNEL", weight=15),
          E("C14_channel_switch", "BEHAVIOUR", weight=5)]
    assert family_risk(ev) == {"CHANNEL": 25, "BEHAVIOUR": 5} and S(ev).risk == 30


def test_behaviour_capped_from_bahul():
    assert S([E(f"C14_t{i}", "BEHAVIOUR", weight=10) for i in range(5)]).risk == 25


def test_risk_capped_at_100_from_bahul():
    assert S([E(f"X_{f}", f, weight=60) for f in ("CHANNEL", "PAYMENT", "SIGNATORY")]).risk == 100


def test_verified_and_unchecked_never_add_risk_from_bahul():
    ev = [E("C3", "SIGNATORY", "unchecked", weight=35, reason="no_data"), E("C1", "COMPANY", "verified", weight=50)]
    assert S(ev).risk == 0


def test_context_family_zero_from_bahul():
    assert S([E("C6_callback", "CONTEXT", weight=99)]).risk == 0


def test_hard_overrides_from_bahul():
    s = S([C1, C2, G1, G3])
    assert s.decision == "BLOCK" and s.rule == "R1 hard fact G3_account_flagged"


def test_low_conf_no_attack_from_bahul():
    s = S([C1_NODATA])
    assert s.decision == "HOLD" and s.rule.startswith("R7") and investigate([C1_NODATA])[0].attack_type != "none"
    assert S([E("C2b", "CHANNEL", "unchecked", reason="no_data")]).rule.startswith("R7")


def test_bank_change_no_account_from_bahul():
    s = S([C1, C2, G1], bank_change=True)
    assert s.decision == "HOLD" and s.rule.startswith("R3")


def test_ambiguous_accounts_hold_from_bahul():
    s = S([C1, C2, G1, E("G5_ambiguous_payment", "PAYMENT", weight=24)])
    assert s.decision == "HOLD" and s.rule.startswith("R3")


@pytest.mark.parametrize("w,decision,rule", [(25, "HOLD", "R4"), (24, "PAY", "R5"), (60, "BLOCK", "R2"),
                                             (59, "HOLD", "R4")])
def test_threshold_boundaries_from_bahul(w, decision, rule):
    """S9: >= for risk thresholds."""
    s = S([C1, C2, G1, E("C9_amount", "COMPANY", weight=w)])
    assert (s.decision, s.rule[:2]) == (decision, rule)


@pytest.mark.parametrize("amount,decision", [(5000.0, "PAY"), (5000.01, "HOLD")])
def test_auto_pay_limit_boundary_from_bahul(amount, decision):
    """S9: <= for the auto-pay limit (MEDIUM)."""
    assert S([C1, C2], amount=amount).decision == decision


def test_medium_fx_conversion_from_bahul():
    assert S([C1, C2], amount=400000, currency="INR").decision == "PAY"        # INR 400k = USD 4,800
    assert S([C1, C2], amount=500000, currency="INR").decision == "HOLD"       # USD 6,000


def test_unknown_currency_never_medium_pay_from_bahul():
    """E9."""
    assert S([C1, C2], amount=10, currency="XYZ").decision == "HOLD"
    assert S([C1, C2], amount=10, currency=None).decision == "HOLD"


def test_geo_money_needs_valid_bank_format_from_bahul():
    c5 = E("C5_geo", "PAYMENT", "verified", confirms="money")
    assert S([C1, C2, c5]).confidence == "HIGH"
    assert S([C1, C2, c5, E("C11_bank_format", "PAYMENT", weight=15)]).confidence == "MEDIUM"


def test_credit_note_holds_from_bahul():
    assert S([C1, C2, G1], credit_note=True).decision == "HOLD"


# ============================================================== test_cli.py

def test_cli_eval_all_match_from_bahul(cached, capsys):
    import cli
    assert cli.main(["eval"]) == 0
    assert "13/13 passed" in capsys.readouterr().out


def test_cli_eval_is_repeatable_from_bahul(cached, capsys):
    """Each eval starts from a fresh graph, so a second run gives the same table."""
    import cli
    cli.main(["eval"])
    first = capsys.readouterr().out
    cli.main(["eval"])
    assert capsys.readouterr().out == first


def test_cli_ablate_from_bahul(cached, capsys):
    """Without G3 the hero is a HOLD: the graph decides it."""
    import cli
    assert cli.main(["ablate"]) == 0
    hero = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("06_hero"))
    assert "G3_account_flagged→HOLD" in hero


def test_cli_gate_commands_from_bahul(cached, capsys, tmp_path, outbox):
    import cli
    assert cli.main(["seed"]) == 0
    f = tmp_path / "in.txt"
    f.write_text(sample("04_new_account_real_domain.txt"), encoding="utf-8")
    capsys.readouterr()
    cli.main(["verify", str(f), "--json"])
    case = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["case_id"]
    cli.main(["pay", case])
    assert "Refused" in capsys.readouterr().out
    cli.main(["resolve", case, "approve", "--by", "asha", "--note", "called vendor"])
    assert "approved" in capsys.readouterr().out
    cli.main(["status", case])
    assert "state APPROVED" in capsys.readouterr().out
    assert not outbox
