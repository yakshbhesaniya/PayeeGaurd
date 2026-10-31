"""Spec 13.1: input and extraction (E1-E16) and X1/X2 integrity rows."""
import pytest
from fakes import sample

from payeeguard.extract import case_id_for, cross_check, extract, normalise_email
from payeeguard.pipeline import analyse


def test_extract_empty():
    v = analyse("   \n\t ", client=None, graph=None, checks=[]).verdict
    assert (v.decision, v.confidence) == ("HOLD", "LOW") and "no content" in v.summary


def test_not_payment_request():
    calls = []
    v = analyse("From: news@example.com\nSubject: Our autumn newsletter\n\nRead our blog about product updates.",
                client=None, graph=None, checks=[("c", "COMPANY", lambda *a: calls.append(1) or [])]).verdict
    assert v.decision == "HOLD" and "no payment request detected" in v.summary and not calls


def test_forwarded_sender():
    inv = extract(sample("02_lookalike.txt")).invoice
    assert inv.sender_email == "rohan.verma@freshw0rks-billing.com"


@pytest.mark.parametrize("marker", ["---------- Forwarded message ---------", "Begin forwarded message:",
                                    "-----Original Message-----"])
def test_forwarded_markers(marker):
    text = f"From: me@buyer.com\nSubject: fwd\n\nplease check\n{marker}\nFrom: X <x@evil.com>\nPay USD 10.00"
    assert extract(text).invoice.sender_email == "x@evil.com"


def test_buyer_domain_top_from_is_forward(monkeypatch):
    from payeeguard import config
    pol = config.policy()
    monkeypatch.setitem(pol["buyer"], "buyer_domain", "buyer.com")
    text = "From: ap@buyer.com\nSubject: see below\n\nFrom: V <v@vendor.com>\nInvoice INV-1 USD 10.00"
    assert extract(text).invoice.sender_email == "v@vendor.com"


def test_quoted_history_ignored():
    text = ("From: a@vendor.com\n\nInvoice INV-9 for USD 100.00.\n> earlier: pay USD 99,999.00 to IBAN "
            "GB33 BUKB 2020 1555 5555 55 urgent\nOn Mon, Bob wrote:\nold text USD 5,000.00 asap")
    ex = extract(text)
    assert ex.invoice.amount == 100.0 and ex.invoice.bank_iban is None and not ex.invoice.urgency_language


def test_html_input():
    text = ("<html><body><p>From: Billing &lt;billing@freshworks.com&gt;</p><p>Invoice INV-7 total "
            "<b>USD&nbsp;4,200.00</b></p></body></html>")
    inv = extract(text).invoice
    assert inv.sender_email == "billing@freshworks.com" and inv.amount == 4200.0 and inv.currency == "USD"


@pytest.mark.parametrize("text,amount,cur", [
    ("USD 4,200.00", 4200.0, "USD"), ("$4,200", 4200.0, "USD"), ("₹3,45,000", 345000.0, "INR"),
    ("Rs. 3,45,000/-", 345000.0, "INR"), ("INR 3.45 lakh", 345000.0, "INR"), ("EUR 9.800,00", 9800.0, "EUR"),
    ("9 800,00 €", 9800.0, "EUR"), ("GBP 1,250.50", 1250.5, "GBP"), ("INR 2 crore", 2e7, "INR"),
])
def test_amount_formats(text, amount, cur):
    inv = extract(f"From: a@b.com\n\nPlease pay {text} for invoice INV-1.").invoice
    assert (inv.amount, inv.currency) == (amount, cur)


def test_dollar_assumed_usd_note():
    assert "currency '$' assumed USD" in extract("From: a@b.com\n\nPay $50 for invoice INV-1").notes


@pytest.mark.parametrize("text", ["Invoice INV-1 attached, please pay to the account on file.",
                                  "Credit note CN-4 for USD 200.00", "Invoice INV-2 for USD 0.00",
                                  "Invoice INV-3 for -USD 300.00"])
def test_amount_missing_holds(text):
    from payeeguard.models import Evidence
    ok = [Evidence("C1", "COMPANY", "c", "verified", "d", "s", confirms="who"),
          Evidence("C2", "CHANNEL", "c", "verified", "d", "s", confirms="channel"),
          Evidence("G1", "PAYMENT", "c", "verified", "d", "s", confirms="money")]
    v = analyse("From: a@vendor.com\n\n" + text, client=None, graph=None,
                checks=[("ok", "COMPANY", lambda *a: ok)]).verdict
    assert v.decision == "HOLD" and v.decision_rule.startswith("R3")


def test_amount_conflict():
    ex = extract("From: a@b.com\nSubject: Invoice INV-1 total USD 4,200.00\n\nTotal: USD 4,800.00")
    rows = [e for e in ex.evidence if e.check == "X2_amount_conflict"]
    assert rows and rows[0].weight == 10 and rows[0].family == "INTEGRITY"


def test_total_preferred_over_line_items():
    inv = extract("From: a@b.com\n\nLicence USD 1,000.00\nSupport USD 200.00\nTotal due: USD 1,200.00").invoice
    assert inv.amount == 1200.0


def test_unknown_currency():
    from payeeguard.models import Evidence
    two = [Evidence("C1", "COMPANY", "c", "verified", "d", "s", confirms="who"),
           Evidence("C2", "CHANNEL", "c", "verified", "d", "s", confirms="channel")]
    v = analyse("From: a@v.com\n\nInvoice INV-5 amount 100", client=None, graph=None,
                checks=[("x", "COMPANY", lambda *a: two)]).verdict
    assert v.decision == "HOLD"     # no currency -> amount missing (R3) or above limit (R7), never PAY


def test_multiple_accounts():
    ex = extract("From: a@b.com\n\nPay USD 10.00 to IBAN GB33 BUKB 2020 1555 5555 55 or account 12345678901")
    rows = [e for e in ex.evidence if e.check == "G5_ambiguous_payment"]
    assert rows and rows[0].weight == 25 and "two different accounts given" in rows[0].detail


def test_same_account_twice_not_ambiguous():
    ex = extract("From: a@b.com\n\nIBAN GB33BUKB20201555555555. Again: IBAN gb33 bukb 2020 1555 5555 55. USD 1.00")
    assert not [e for e in ex.evidence if e.check == "G5_ambiguous_payment"]


def test_account_on_file(graph):
    from fakes import FAKE_CHECKS
    text = ("From: Billing <billing@freshworks.com>\nSubject: Invoice INV-2026-1050\n\nInvoice INV-2026-1050 for "
            "USD 4,200.00. Please pay to our account on file.\nVendor: Freshworks Inc, freshworks.com")
    c = analyse(text, client=None, graph=graph, checks=FAKE_CHECKS)
    assert any(e.check == "E11_account_on_file" and e.confirms == "money" for e in c.verdict.evidence)
    assert c.verdict.decision == "PAY"
    other = text.replace("freshworks", "acmewidgets").replace("Freshworks", "Acmewidgets")
    unknown = analyse(other, client=None, graph=graph, checks=FAKE_CHECKS).verdict
    assert unknown.decision == "HOLD"


def test_bank_normalisation():
    inv = extract("From: a@b.com\n\nIBAN: gb33 bukb 2020 1555 5555 55 SWIFT: bukbgb22 account no 12-3456-7890 USD 1.00").invoice
    assert inv.bank_iban == "GB33BUKB20201555555555" and inv.bank_swift == "BUKBGB22"
    inv2 = extract("From: a@b.com\n\nAccount number: 12-3456-7890 USD 1.00").invoice
    assert inv2.bank_account == "1234567890" and inv2.bank_account_last4 == "7890"


def test_iban_does_not_swallow_next_word():
    inv = extract("From: a@b.com\n\nIBAN: DE89 3704 0044 0532 0130 00 SWIFT: COBADEFFXXX EUR 5,00").invoice
    assert inv.bank_iban == "DE89370400440532013000"


@pytest.mark.parametrize("raw,want", [
    ("billing [at] freshworks.com", "billing@freshworks.com"), ("mailto:Billing@Freshworks.com", "billing@freshworks.com"),
    ("billing+inv@freshworks.com", "billing@freshworks.com"), ("billing@freshworks.com.", "billing@freshworks.com"),
    ("BILLING@FRESHWORKS.COM", "billing@freshworks.com"), ("billing (at) freshworks [dot] com", "billing@freshworks.com"),
])
def test_email_normalisation(raw, want):
    assert normalise_email(raw) == want


def test_hinglish_tactics():
    inv = extract("From: a@b.com\n\nBhai turant payment karo, abhi pay karo. Invoice INV-1 Rs. 5,000/-").invoice
    assert inv.urgency_language and inv.amount == 5000.0


def test_ocr_noise_amount():
    inv = extract("From: a@b.com\n\nInv0ice INV-77 T0tal: USD 4,200.00 IBAN: GB33 BUKB 2020 1555 5555 55").invoice
    assert inv.amount == 4200.0 and inv.bank_iban == "GB33BUKB20201555555555"


def test_huge_input():
    big = "From: a@b.com\n\nInvoice INV-1 for USD 10.00\n" + "x" * 50_000
    assert len(extract(big).flags["body"]) <= 20_000
    assert case_id_for(big) != case_id_for(big[:20_000])           # hash covers the full text


def test_field_truncation():
    ex = cross_check(extract(sample("01_legit.txt")), {"vendor_name": "A" * 5000, "sender_title": "B" * 5000})
    assert len(ex.invoice.sender_title) == 200
    assert ex.invoice.vendor_name == "Freshworks Inc"              # regex value kept


def test_server_x1_mismatch():
    """M2 / sample 08: injected sender is ignored; X1 shows both values; regex wins."""
    ex = cross_check(extract(sample("08_prompt_injection.txt")), {"sender_email": "billing@freshworks.com"})
    assert ex.invoice.sender_email == "accounts@freshworks-invoices.com"
    x1 = [e for e in ex.evidence if e.check == "X1_extraction"]
    assert x1 and "billing@freshworks.com" in x1[0].detail and x1[0].weight == 10


def test_x1_value_not_in_text_ignored():
    ex = cross_check(extract("From: a@b.com\n\nInvoice INV-1 USD 10.00"), {"bank_iban": "GB33BUKB20201555555555"})
    assert ex.invoice.bank_iban is None and ex.evidence[0].check == "X1_extraction"
    assert "5555" in ex.evidence[0].detail and "GB33BUKB" not in ex.evidence[0].detail


def test_x1_fills_gap_when_value_is_in_text():
    ex = cross_check(extract("Please pay invoice INV-1 USD 10.00 from billing@vendor.com"),
                     {"vendor_name": "Vendor Ltd", "bank_change_requested": True})
    assert ex.invoice.vendor_name == "Vendor Ltd" and ex.invoice.bank_change_requested


def test_llm_cannot_clear_caution_flags():
    ex = cross_check(extract(sample("05_zoho_lookalike_mule.txt")), {"bank_change_requested": False})
    assert ex.invoice.bank_change_requested


@pytest.mark.parametrize("name,country", [("01_legit.txt", "US"), ("02_lookalike.txt", "NG"), ("03_gmail_signatory.txt", "IN"),
                                          ("05_zoho_lookalike_mule.txt", "GB"), ("07_unknown_vendor.txt", "IN"),
                                          ("08_prompt_injection.txt", "DE")])
def test_remit_country(name, country):
    assert extract(sample(name)).invoice.remit_country == country


def test_injected_vendor_domain_ignored():
    """Claude can't steer C1 to another company with a domain that is not in the email."""
    ex = cross_check(extract(sample("05_zoho_lookalike_mule.txt")), {"vendor_domain": "freshworks.com"})
    assert ex.invoice.vendor_domain is None
    assert any(e.check == "X1_extraction" and "vendor_domain" in e.detail for e in ex.evidence)
    ok = cross_check(extract("From: a@b.com\n\nInvoice INV-1 USD 5.00 from acme.com"), {"vendor_domain": "ACME.com"})
    assert ok.invoice.vendor_domain == "acme.com" and not ok.evidence


def test_non_iban_codes_not_taken_as_iban():
    inv = extract("From: a@b.com\n\nOrder ZZ12 3456 7890 1234 56 and ref AB12CD34EF56GH78 for USD 5.00").invoice
    assert inv.bank_iban is None


def test_invalid_checksum_iban_still_extracted():
    """C11 must see a bad IBAN to flag it, so extraction keeps it."""
    assert extract("From: a@b.com\n\nIBAN: GB00 WEST 1234 5698 7654 32 USD 5.00").invoice.bank_iban == \
        "GB00WEST12345698765432"


@pytest.mark.parametrize("junk", ["a" * 20000, "1," * 10000, "GB82 " * 4000, "@" * 20000, "On " * 7000,
                                  ("x" * 50 + "\n") * 400, "From: " + "<" * 20000, "USD " + "9 " * 10000])
def test_regex_no_catastrophic_backtracking(junk):
    import time
    t = time.perf_counter()
    extract("From: a@b.com\n\n" + junk)
    assert time.perf_counter() - t < 2.0
