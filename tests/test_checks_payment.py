"""PAYMENT checks G1-G5, C9 (spec 7, 13.1 E10/E11, 13.4) on the seeded graph and the samples."""
from __future__ import annotations

import pytest

from conftest import FRESHWORKS, ZOHO, inv, sample
from payeeguard import checks_payment as cp
from payeeguard.checks_payment import accounts_in_text, check_payment, iban_valid, policy
from payeeguard.graph import case_id_for

FW = f"company:{FRESHWORKS}"
MULE = "GB82 WEST 1234 5698 7654 32"


def by_check(rows):
    out = {}
    for r in rows:
        assert r.check not in out, f"duplicate row {r.check}"
        out[r.check] = r
    return out


def run(graph, name=None, invoice=None, ctx=None):
    if name:
        invoice, ctx = sample(name)
    return by_check(check_payment(invoice, ctx, None, graph)), ctx


def assert_well_formed(rows):
    for r in rows.values():
        assert r.family == "PAYMENT" and r.source and r.detail
        if r.status == "unchecked":
            assert r.unchecked_reason and r.weight == 0 and not r.hard_block
        if r.status == "verified":
            assert r.weight == 0 and not r.hard_block
        if r.confirms:
            assert r.status == "verified"


# ---------------------------------------------------------------- the samples, in eval order

def test_sample_01_legit_pays(seeded):
    rows, ctx = run(seeded, "01_legit.txt")
    assert_well_formed(rows)
    assert rows["G1_account_history"].status == "verified" and rows["G1_account_history"].confirms == "money"
    assert "14 time(s)" in rows["G1_account_history"].detail
    assert rows["G2_account_other_vendor"].status == "verified"
    assert rows["G3_account_flagged"].status == "verified"
    assert rows["G4_duplicate"].status == "verified"
    assert rows["C9_amount"].status == "verified"
    assert rows["G5_ambiguous_payment"].status == "verified"
    assert not any(r.status == "contradicted" for r in rows.values())
    assert ctx["vendor_known"] is True


def test_sample_04_new_account_holds(seeded):
    rows, _ = run(seeded, "04_new_account_real_domain.txt")
    g1 = rows["G1_account_history"]
    assert g1.status == "contradicted" and g1.weight == 25 and not g1.hard_block
    assert "••6789 US" in g1.detail and "••5555 GB" in g1.detail
    assert rows["G3_account_flagged"].status == "verified"
    assert not any(r.hard_block for r in rows.values())


def test_hero_05_then_06(seeded):
    """The demo: 05 is blocked (gate writes FLAGGED_IN); 06 from the real domain hits G3 hard + G1 25."""
    rows5, c5 = run(seeded, "05_zoho_lookalike_mule.txt")
    assert rows5["G3_account_flagged"].status == "verified"           # nothing known yet
    assert rows5["G1_account_history"].status == "contradicted"       # Zoho is always paid to its HDFC account
    seeded.record_block(c5["case_id"], seeded.fingerprint(iban=MULE), label="Zoho impersonation BLOCKED")
    rows6, c6 = run(seeded, "06_hero_freshworks_mule.txt")
    g3 = rows6["G3_account_flagged"]
    assert g3.status == "contradicted" and g3.hard_block
    assert c5["case_id"][:4] in g3.detail and "Zoho impersonation" in g3.detail
    assert g3.source.startswith("graph:FLAGGED_IN/account:")
    assert rows6["G1_account_history"].status == "contradicted" and rows6["G1_account_history"].weight == 25
    assert c6["related_cases"] == [c5["case_id"]]
    rows5b, _ = run(seeded, "05_zoho_lookalike_mule.txt")             # G-1: 05 re-run doesn't match itself
    assert rows5b["G3_account_flagged"].status == "verified"


def test_sample_07_unknown_vendor_is_not_fraud(seeded):
    rows, ctx = run(seeded, "07_unknown_vendor.txt")
    assert_well_formed(rows)
    assert not any(r.status == "contradicted" for r in rows.values())
    assert rows["G1_account_history"].unchecked_reason == "no_data"
    assert ctx["vendor_known"] is False


def test_duplicate_new_text(seeded):
    """G-4 / sample 09: INV-2026-1031 is in the seed, same account -> 40 (HOLD), not hard."""
    rows, _ = run(seeded, "09_duplicate_invoice.txt")
    g4 = rows["G4_duplicate"]
    assert g4.status == "contradicted" and g4.weight == 40 and not g4.hard_block
    assert "2025-11-03" in g4.detail
    assert rows["G1_account_history"].status == "verified"


def test_duplicate_to_new_account_is_hard(seeded):
    i = inv(vendor_domain="freshworks.com", invoice_number="inv-2026-1031", amount=4200.0, currency="USD",
            bank_iban="GB33 BUKB 2020 1555 5555 55", raw_text="x")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["G4_duplicate"].hard_block


def test_duplicate_without_invoice_number(seeded):
    i = inv(vendor_domain="freshworks.com", amount=4210.0, currency="USD", invoice_date="2026-10-10",
            bank_account="000123456789", bank_swift="CHASUS33", raw_text="y")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["G4_duplicate"].weight == 15 and rows["G4_duplicate"].status == "contradicted"
    i.invoice_date = "2026-12-20"
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS, "case_id": "zz"})
    assert rows["G4_duplicate"].status == "verified"


# ---------------------------------------------------------------- E11 / S5: no account in the email

def test_account_on_file(seeded):
    known = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", invoice_number="INV-9",
                raw_text="pay to account on file")
    rows, ctx = run(seeded, invoice=known, ctx={"company_id": FRESHWORKS})
    assert rows["G1_account_history"].confirms == "money" and ctx["ledger_account"] == "••6789 US"
    unknown = inv(vendor_domain="newco.example", amount=10.0, currency="USD", raw_text="pay to account on file")
    rows, _ = run(seeded, invoice=unknown, ctx={})
    assert rows["G1_account_history"].status == "unchecked" and rows["G1_account_history"].confirms is None


def test_bank_change_no_account(seeded):
    i = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", bank_change_requested=True,
            raw_text="our account changed, details to follow")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["G1_account_history"].status == "unchecked"
    assert rows["G1_account_history"].unchecked_reason == "not_applicable"


# ---------------------------------------------------------------- G2, G5, weak

def test_account_paid_to_other_vendor(seeded):
    i = inv(vendor_domain="zoho.com", amount=210000.0, currency="INR", invoice_number="ZH-1",
            bank_account="000123456789", bank_swift="CHASUS33", raw_text="z")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": ZOHO})
    g2 = rows["G2_account_other_vendor"]
    assert g2.status == "contradicted" and g2.weight == 30 and not g2.hard_block and "Freshworks" in g2.detail


def test_shared_account_allowlist(seeded):
    """G-6: an allow-listed shared account doesn't raise G2."""
    fp = seeded.fingerprint(account="000123456789", code="CHASUS33")
    pol = {k: dict(v) for k, v in policy().items()}
    pol["payment"]["known_shared_accounts"] = [fp.strong[0].split(":", 1)[1]]
    i = inv(vendor_domain="zoho.com", amount=210000.0, currency="INR", bank_account="000123456789",
            bank_swift="CHASUS33", raw_text="z2")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": ZOHO, "policy": pol})
    g2 = rows["G2_account_other_vendor"]
    assert g2.status == "verified" and "allow-list" in g2.detail


def test_weak_fp_never_hard(seeded):
    """G-5: last-4 only: usable for G1, never G2/G3 even if the strong account is flagged."""
    seeded.record_block("caseX", seeded.fingerprint(account="000123456789", code="CHASUS33"), label="x")
    i = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", bank_swift="CHASUS33",
            bank_account_last4="6789", raw_text="w")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["G1_account_history"].status == "verified" and "weak" in rows["G1_account_history"].detail
    assert rows["G2_account_other_vendor"].status == "unchecked"
    assert rows["G3_account_flagged"].status == "unchecked"
    assert not any(r.hard_block for r in rows.values())
    other = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", bank_swift="CHASUS33",
                bank_account_last4="1111", raw_text="w2")
    rows, _ = run(seeded, invoice=other, ctx={"company_id": FRESHWORKS})
    assert rows["G1_account_history"].status == "contradicted"


def test_account_without_bank_code_matches_history(seeded):
    i = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", bank_account="0001 2345 6789", raw_text="n")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["G1_account_history"].status == "verified"


def test_codeless_number_in_blocked_case_is_not_hard(seeded):
    """Same account number, bank code missing on one side: two banks can share it -> 30, never hard."""
    seeded.record_block("caseM", seeded.fingerprint(account="44445555666", code="BARCGB22"), label="mule")
    i = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", bank_account="44445555666", raw_text="m")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    g3 = rows["G3_account_flagged"]
    assert g3.status == "contradicted" and g3.weight == 30 and not g3.hard_block
    same = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", bank_account="44445555666",
               bank_swift="BARCGB22", raw_text="m2")
    rows, _ = run(seeded, invoice=same, ctx={"company_id": FRESHWORKS})
    assert rows["G3_account_flagged"].hard_block
    other_bank = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", bank_account="44445555666",
                     bank_swift="HSBCGB2L", raw_text="m3")
    rows, _ = run(seeded, invoice=other_bank, ctx={"company_id": FRESHWORKS})
    assert rows["G3_account_flagged"].status == "verified"      # same number, different bank: not the account


def test_same_number_other_bank_is_not_history(seeded):
    i = inv(vendor_domain="freshworks.com", amount=4200.0, currency="USD", bank_account="000123456789",
            bank_swift="HSBCGB2L", invoice_number="INV-2026-1031", raw_text="o")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["G1_account_history"].status == "contradicted"
    assert rows["G4_duplicate"].hard_block                      # already paid, now to another bank


def test_multiple_accounts(seeded):
    """E10: two different accounts in one email -> G5 25."""
    text = ("Pay to IBAN GB82 WEST 1234 5698 7654 32.\n"
            "Alternatively use IBAN DE89 3704 0044 0532 0130 00.")
    i = inv(vendor_domain="freshworks.com", amount=10.0, currency="USD", bank_iban=MULE, raw_text=text)
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    g5 = rows["G5_ambiguous_payment"]
    assert g5.status == "contradicted" and g5.weight == 25 and "••5432 GB" in g5.detail and "••3000 DE" in g5.detail


def test_same_account_twice_is_not_ambiguous(seeded):
    text = ("IBAN: GB82 WEST 1234 5698 7654 32 (account 98765432)\n"
            "IBAN gb82west12345698765432 again for reference")
    i = inv(bank_iban=MULE, raw_text=text, vendor_domain="freshworks.com")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["G5_ambiguous_payment"].status == "verified"


def test_quoted_history_accounts_ignored():
    text = "Pay account 000123456789.\nOn Oct 2, Freshworks wrote:\n> account 999988887777\n"
    assert accounts_in_text(text) == [{"account": "000123456789"}]
    assert accounts_in_text("> IBAN GB82 WEST 1234 5698 7654 32") == []


def test_g5_uses_extractor_accounts(seeded):
    i = inv(vendor_domain="freshworks.com", raw_text="see attachment")
    ctx = {"company_id": FRESHWORKS, "accounts": [{"iban": MULE}, {"account": "123405000789", "swift": "ICIC0001234"}]}
    rows, _ = run(seeded, invoice=i, ctx=ctx)
    assert rows["G5_ambiguous_payment"].status == "contradicted"


@pytest.mark.parametrize("iban,ok", [(MULE, True), ("DE89 3704 0044 0532 0130 00", True),
                                     ("GB33BUKB20201555555555", True), ("GB82 WEST 1234 5698 7654 33", False),
                                     ("INV-2026-1043", False), ("", False)])
def test_iban_valid(iban, ok):
    assert iban_valid(iban) is ok


# ---------------------------------------------------------------- C9

def test_amount_anomaly(seeded):
    i = inv(vendor_domain="freshworks.com", amount=18400.0, currency="USD", invoice_number="N1",
            bank_account="000123456789", bank_swift="CHASUS33", raw_text="big")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["C9_amount"].status == "contradicted" and rows["C9_amount"].weight == 10


def test_amount_cross_currency_uses_fx(seeded):
    i = inv(vendor_domain="freshworks.com", amount=4000.0, currency="EUR", raw_text="eur",
            bank_account="000123456789", bank_swift="CHASUS33")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["C9_amount"].status == "verified" and "USD" in rows["C9_amount"].detail
    i2 = inv(vendor_domain="freshworks.com", amount=4000.0, currency="XYZ", raw_text="xyz")
    rows, _ = run(seeded, invoice=i2, ctx={"company_id": FRESHWORKS})
    assert rows["C9_amount"].status == "unchecked"


@pytest.mark.parametrize("amount", [None, 0.0, -50.0])
def test_amount_missing_is_not_applicable(seeded, amount):
    i = inv(vendor_domain="freshworks.com", amount=amount, currency="USD", raw_text=f"a{amount}")
    rows, _ = run(seeded, invoice=i, ctx={"company_id": FRESHWORKS})
    assert rows["C9_amount"].status == "unchecked" and rows["C9_amount"].weight == 0


def test_too_little_history(graph):
    graph.record_payment("p1", "vendor:tiny.example", graph.fingerprint(account="12345678", code="ICIC0001234"),
                         "T-1", 100.0, "INR", "tiny.example", "2026-01-01")
    i = inv(vendor_domain="tiny.example", amount=1000.0, currency="INR", raw_text="t")
    rows, _ = run(graph, invoice=i, ctx={})
    assert rows["C9_amount"].unchecked_reason == "no_data"


# ---------------------------------------------------------------- S2 small vendor, robustness

def test_small_vendor_known_in_graph(graph):
    fp = graph.fingerprint(account="12345678", code="ICIC0001234")
    graph.record_payment("p1", "vendor:tiny.example", fp, "T-1", 100.0, "INR", "tiny.example", "2026-01-01")
    i = inv(vendor_domain="tiny.example", amount=100.0, currency="INR", invoice_number="T-2",
            bank_account="12345678", bank_swift="ICIC0001234", raw_text="tiny")
    rows, ctx = run(graph, invoice=i, ctx={})
    assert rows["G1_account_history"].confirms == "money" and ctx["vendor_known"]


def test_no_graph_is_unchecked():
    i, ctx = sample("01_legit.txt")
    rows = by_check(check_payment(i, ctx, None, None))
    assert len(rows) == 6
    assert all(r.status == "unchecked" and r.unchecked_reason == "api_error" for r in rows.values())


def test_check_crash_isolated(seeded, monkeypatch):
    """S10: one check raising doesn't stop the others."""
    def boom(*a, **k):
        raise RuntimeError("000123456789")         # message must not leak into evidence
    monkeypatch.setattr(cp, "CHECKS", [(c, cl, boom if c == "G3_account_flagged" else fn) for c, cl, fn in cp.CHECKS])
    i, ctx = sample("01_legit.txt")
    rows = by_check(check_payment(i, ctx, None, seeded))
    assert rows["G3_account_flagged"].unchecked_reason == "api_error"
    assert "000123456789" not in rows["G3_account_flagged"].detail
    assert rows["G1_account_history"].status == "verified"


def test_no_full_account_in_evidence(seeded):
    """Y2 for evidence rows: only country + last 4 ever appear."""
    for name in ("01_legit.txt", "04_new_account_real_domain.txt", "06_hero_freshworks_mule.txt",
                 "07_unknown_vendor.txt", "09_duplicate_invoice.txt"):
        i, ctx = sample(name)
        for r in check_payment(i, ctx, None, seeded):
            for full in ("000123456789", "123405000789", "GB82WEST", "WEST 1234", "GB33BUKB", "BUKB 2020"):
                assert full not in r.detail and full not in r.source


def test_case_id_filled_when_missing(seeded):
    i = inv(vendor_domain="freshworks.com", raw_text="hello")
    ctx: dict = {"company_id": FRESHWORKS}
    check_payment(i, ctx, None, seeded)
    assert ctx["case_id"] == case_id_for("hello")


def test_policy_defaults(monkeypatch, tmp_path):
    """M7: missing or invalid policy.toml -> built-in defaults."""
    bad = tmp_path / "policy.toml"
    bad.write_text("this is = = not toml", encoding="utf-8")
    cp._load_policy.cache_clear()
    pol = cp._load_policy(str(bad))
    assert pol["payment"]["amount_anomaly_multiple"] == 3.0
    assert cp._load_policy(str(tmp_path / "missing.toml"))["payment"]["duplicate_window_days"] == 14
    cp._load_policy.cache_clear()
    assert policy()["payment"]["duplicate_window_days"] == 14        # real file still loads
