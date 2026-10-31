"""Scenarios ported from Aditya's suites (test_audit, test_checks_payment, test_checks_signatory, test_graph).

Each test keeps the behaviour of the original, re-expressed against the integrated API and the offline
conftest harness. Sample-based tests replay samples/*.txt through the committed .cache (PAYEEGUARD_OFFLINE=1),
the same way `cli.py eval` does, on a fresh graph seeded from samples/seed_history.json.
Tests marked xfail(strict=True, reason="BUG: ...") document real defects in the integrated code.
"""

import json
import sqlite3
import threading
from datetime import date, timedelta

import httpx
import pytest

from conftest import FW, email, seed_freshworks
from payeeguard import audit, config, gate, geo
from payeeguard.client import CrustdataClient
from payeeguard.extract import case_id_for, extract
from payeeguard.graph import Graph, seed
from payeeguard.pipeline import verify

FWN = f"company:{FW}"
MULE = "GB82 WEST 1234 5698 7654 32"
HERO = f"IBAN {MULE} SWIFT WESTGB2L"
ON_FILE = "Pay to account on file: Chase SWIFT CHASUS33 account 000123456789"


def checks(v):
    return {e.check: e for e in v.evidence}


def sample_text(name):
    return (config.ROOT / "samples" / name).read_text(encoding="utf-8")


@pytest.fixture
def outbox(monkeypatch):
    """Capture gate payments instead of writing the repo's payments/outbox.jsonl."""
    rows = []
    monkeypatch.setattr(gate, "_outbox", rows.append)
    return rows


@pytest.fixture
def sg(env, monkeypatch):
    """(client, graph) replaying the committed Crustdata cache offline, graph seeded like `cli.py eval`."""
    monkeypatch.setenv("PAYEEGUARD_OFFLINE", "1")
    monkeypatch.setenv("PAYEEGUARD_CACHE_DIR", str(config.ROOT / ".cache"))
    c = CrustdataClient(api_key="test-key")
    g = Graph(env / "eval_graph.db")
    seed(g, c)
    yield c, g
    g.close()


def run(sg, name, save=False, llm=None):
    c, g = sg
    return verify(sample_text(name), llm, client=c, graph=g, save=save)[0]


# ============================================================== test_audit.py

def _events(n=3):
    return [audit.append("verdict", case_id=f"c{i}", decision="HOLD") for i in range(n)]


def _audit_path(env):
    return env / "reports" / "audit.jsonl"


def test_append_and_verify_from_aditya(env):
    hashes = _events(3)
    ok, msg = audit.verify()
    assert ok and "intact" in msg and "3 events" in msg
    lines = [json.loads(x) for x in _audit_path(env).read_text(encoding="utf-8").splitlines()]
    assert lines[0]["prev_hash"] == "0" * 64 and lines[1]["prev_hash"] == hashes[0]
    assert lines[-1]["hash"] == hashes[-1]


def test_audit_tamper_detected_delete_and_reorder_from_aditya(env):
    """P6: deleting or reordering lines is reported (editing is covered by test_P6 in test_edge_cases)."""
    _events(4)
    p = _audit_path(env)
    original = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join(original[:1] + original[2:]) + "\n", encoding="utf-8")
    ok, msg = audit.verify()
    assert not ok and "line 2" in msg
    p.write_text("\n".join([original[1], original[0]] + original[2:]) + "\n", encoding="utf-8")
    ok, msg = audit.verify()
    assert not ok and "line 1" in msg


def test_audit_tamper_detected_garbage_line_from_aditya(env):
    _events(4)
    p = _audit_path(env)
    original = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join(original[:2] + ["{not json"] + original[2:]) + "\n", encoding="utf-8")
    ok, msg = audit.verify()
    assert not ok and "line 3" in msg


def test_truncating_the_tail_is_undetectable_but_chain_stays_valid_from_aditya(env):
    """Documented limit: dropping the newest lines leaves a valid prefix."""
    hashes = _events(3)
    p = _audit_path(env)
    kept = p.read_text(encoding="utf-8").splitlines()[:2]
    p.write_text("\n".join(kept) + "\n", encoding="utf-8")
    assert audit.verify()[0] and json.loads(kept[-1])["hash"] != hashes[-1]


def test_missing_log_is_ok_from_aditya(env):
    ok, msg = audit.verify()
    assert ok and "empty" in msg


def test_sanitise_never_writes_secrets_from_aditya(client, graph, env, outbox):
    """Y1/Y2 for the audit log: after verify(save=True) + pay, no raw text or full account number in audit.jsonl."""
    seed_freshworks(graph)
    v, _ = verify(email(body=ON_FILE), client=client, graph=graph, save=True)
    assert v.decision == "PAY"
    gate.pay_invoice(v.case_id, graph)
    text = _audit_path(env).read_text(encoding="utf-8")
    for leaked in ("Please pay invoice", "000123456789", "test-key"):
        assert leaked not in text
    assert audit.verify()[0]


def test_concurrent_appends_keep_one_chain_from_aditya(env):
    def writer():
        for i in range(15):
            audit.append("verdict", i=i)
    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok, msg = audit.verify()
    assert ok and "60 events" in msg, msg


def test_damaged_line_does_not_stop_logging_from_aditya(client, graph, env):
    _events(1)
    with open(_audit_path(env), "a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    v, _ = verify(email(), client=client, graph=graph, save=True)     # still returns a verdict
    assert v.decision in ("PAY", "HOLD", "BLOCK")
    assert not audit.verify()[0]                                      # and verify reports the damage


def test_unicode_and_cli_from_aditya(env, capsys):
    import cli
    audit.append("note", reviewer="Opéra ₹")
    assert "Opéra ₹" in _audit_path(env).read_text(encoding="utf-8")
    assert cli.main(["audit"]) == 0
    assert "intact" in capsys.readouterr().out


# ============================================================== test_checks_payment.py

def assert_well_formed(rows):
    for r in rows:
        assert r.source and r.detail
        if r.status == "unchecked":
            assert r.unchecked_reason and r.weight == 0 and not r.hard_block
        if r.status == "verified":
            assert not r.hard_block
        if r.confirms:
            assert r.status == "verified"


def test_sample_01_legit_pays_from_aditya(sg):
    v = run(sg, "01_legit.txt")
    assert_well_formed(v.evidence)
    c = checks(v)
    assert c["G1_account_history"].status == "verified" and c["G1_account_history"].confirms == "money"
    assert "14 of 14" in c["G1_account_history"].detail
    assert c["C9_amount"].status == "verified"
    assert not any(e.status == "contradicted" for e in v.evidence if e.family == "PAYMENT")
    assert v.decision == "PAY"


def test_sample_04_new_account_holds_from_aditya(sg):
    v = run(sg, "04_new_account_real_domain.txt")
    g1 = checks(v)["G1_account_history"]
    assert g1.status == "contradicted" and g1.weight == 25 and not g1.hard_block
    assert "••••6789 US" in g1.detail and "••••5555 GB" in g1.detail
    assert "G3_account_flagged" not in checks(v)
    assert not any(e.hard_block for e in v.evidence)


def test_hero_05_then_06_from_aditya(sg):
    """05 is blocked (FLAGGED_IN written); 06 from the real domain hits G3 hard + G1 25; re-running 05 is clean."""
    v5 = run(sg, "05_zoho_lookalike_mule.txt")
    assert v5.decision == "BLOCK" and "G3_account_flagged" not in checks(v5)
    # (Aditya also expected G1 contradicted on 05; integrated C1 finds "Zoho Corporation" ambiguous by name (D7),
    # so the lookalike is never tied to Zoho's history and G1 is unchecked: dropped.)
    v6 = run(sg, "06_hero_freshworks_mule.txt")
    g3 = checks(v6)["G3_account_flagged"]
    assert g3.status == "contradicted" and g3.hard_block
    assert v5.case_id in g3.detail and v5.case_id in g3.source
    assert g3.source.startswith("graph:FLAGGED_IN/account:")
    assert checks(v6)["G1_account_history"].status == "contradicted" and checks(v6)["G1_account_history"].weight == 25
    assert any(e.get("rel") == "FLAGGED_IN" and v5.case_id in e["dst_label"] for e in v6.graph_edges)
    v5b = run(sg, "05_zoho_lookalike_mule.txt")       # G-1: never matches its own FLAGGED_IN, only case 06's
    g3b = checks(v5b)["G3_account_flagged"]
    assert v5.case_id not in g3b.source and v6.case_id in g3b.source


def test_sample_07_unknown_vendor_is_not_fraud_from_aditya(sg):
    v = run(sg, "07_unknown_vendor.txt")
    assert_well_formed(v.evidence)
    assert not any(e.status == "contradicted" for e in v.evidence if e.family == "PAYMENT")
    assert checks(v)["G1_account_history"].status == "unchecked"
    assert v.decision == "HOLD"


def test_duplicate_new_text_from_aditya(sg):
    """G-4 / sample 09: INV-2026-1031 is in the seed, same account -> 40, not hard."""
    v = run(sg, "09_duplicate_invoice.txt")
    g4 = checks(v)["G4_duplicate"]
    assert g4.status == "contradicted" and g4.weight == 40 and not g4.hard_block
    assert "2025-11-03" in g4.detail
    assert checks(v)["G1_account_history"].status == "verified"


def test_duplicate_to_new_account_is_hard_from_aditya(client, graph):
    seed_freshworks(graph, n=14)          # seeds INV-2026-1029..1042
    raw = email(body="Remit to IBAN GB33 BUKB 2020 1555 5555 55").replace("INV-2026-1043", "INV-2026-1031")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    g4 = checks(v)["G4_duplicate"]
    assert g4.hard_block and "different account" in g4.detail and v.decision == "BLOCK"


def test_duplicate_invoice_number_case_variant_from_aditya(client, graph):
    seed_freshworks(graph, n=14)
    raw = email(body="Remit to IBAN GB33 BUKB 2020 1555 5555 55").replace("INV-2026-1043", "inv-2026-1031")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert checks(v).get("G4_duplicate") and checks(v)["G4_duplicate"].status == "contradicted"


def _no_invoice_no(amount="USD 4,210.00", body=ON_FILE):
    return (f"From: Freshworks Billing <billing@freshworks.com>\nSubject: Monthly bill\n\n"
            f"Please pay {amount} for this month's subscription.\nVendor: Freshworks Inc, freshworks.com\n{body}")


def test_duplicate_without_invoice_number_from_aditya(client, graph):
    """Same amount +-1% within duplicate_window_days and no invoice number -> G4 15; outside the window -> nothing."""
    fp = seed_freshworks(graph)
    recent = (date.today() - timedelta(days=3)).isoformat()
    graph.record_payment("recent", FWN, "INV-2026-1099", 4200.0, "USD", fp, recent, "test", "freshworks.com")
    v, _ = verify(_no_invoice_no(), client=client, graph=graph, save=False)
    g4 = checks(v)["G4_duplicate"]
    assert g4.status == "contradicted" and g4.weight == 15 and not g4.hard_block


def test_duplicate_without_invoice_number_outside_window_from_aditya(client, graph):
    seed_freshworks(graph)                 # newest seeded payment is weeks old
    v, _ = verify(_no_invoice_no(), client=client, graph=graph, save=False)
    assert "G4_duplicate" not in checks(v)


def test_account_on_file_unknown_vendor_from_aditya(client, graph):
    """E11: 'account on file' only confirms money for a vendor with history; unknown vendor -> unchecked, HOLD."""
    raw = ("From: Accounts <accounts@newco-widgets.io>\nSubject: Invoice NW-2026-001\n\n"
           "Please pay invoice NW-2026-001 for USD 10.00 to the account on file.\nVendor: Newco Widgets")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    g1 = checks(v)["G1_account_history"]
    assert g1.status == "unchecked" and g1.confirms is None and v.decision == "HOLD"


def test_shared_account_allowlist_from_aditya(client, graph, monkeypatch):
    """G-6: an allow-listed shared account doesn't raise G2."""
    seed_freshworks(graph)
    fp = graph.fingerprint(iban=MULE)
    graph.record_payment("other", "company:123", "X-1", 10.0, "USD", fp, "2026-01-01", "test", "other.com")
    monkeypatch.setitem(config.policy()["payment"], "known_shared_accounts", [fp.id])
    v, _ = verify(email(body=f"Pay {HERO}"), client=client, graph=graph, save=False)
    assert "G2_account_other_vendor" not in checks(v)


def test_weak_fp_matches_history_from_aditya(client, graph):
    """G-5: last-4 + SWIFT is usable for G1 history; a different last-4 is a new account."""
    seed_freshworks(graph)
    v, _ = verify(email(body="Pay to SWIFT CHASUS33 account ending 6789"), client=client, graph=graph, save=False)
    assert checks(v)["G1_account_history"].status == "verified"
    assert not any(e.hard_block for e in v.evidence)
    v, _ = verify(email(body="Pay to SWIFT CHASUS33 account ending 1111"), client=client, graph=graph, save=False)
    assert checks(v)["G1_account_history"].status == "contradicted"


def test_account_without_bank_code_new_account_not_paid_from_aditya(client, graph):
    seed_freshworks(graph)
    v, _ = verify(email(body="Please pay to account 99998888777."), client=client, graph=graph, save=False)
    assert v.decision != "PAY"
    assert checks(v)["G1_account_history"].status == "contradicted"


def test_codeless_number_in_blocked_case_same_bank_is_hard_from_aditya(client, graph):
    """Same number + same bank code as a blocked case -> hard; same number at another bank -> not that account."""
    seed_freshworks(graph)
    graph.flag("caseM", [graph.fingerprint(account="44445555666", code="BARCGB22").id], "test")
    v, _ = verify(email(body="Pay SWIFT BARCGB22 account 44445555666"), client=client, graph=graph, save=False)
    assert checks(v)["G3_account_flagged"].hard_block
    v, _ = verify(email(body="Pay SWIFT HSBCGB2L account 44445555666"), client=client, graph=graph, save=False)
    assert "G3_account_flagged" not in checks(v)


def test_same_number_other_bank_is_not_history_from_aditya(client, graph):
    seed_freshworks(graph)
    raw = email(body="Pay SWIFT HSBCGB2L account 000123456789").replace("INV-2026-1043", "INV-2026-1031")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert checks(v)["G1_account_history"].status == "contradicted"
    assert checks(v)["G4_duplicate"].hard_block                   # already paid, now to another bank


def test_same_account_twice_is_not_ambiguous_from_aditya(client, graph):
    seed_freshworks(graph)
    body = "IBAN: GB82 WEST 1234 5698 7654 32\nIBAN gb82west12345698765432 again for reference"
    v, _ = verify(email(body=body), client=client, graph=graph, save=False)
    assert "G5_ambiguous_payment" not in checks(v)


def test_quoted_history_accounts_ignored_from_aditya():
    _, meta = extract("From: a@b.com\n\nPay USD 10 to account 000123456789.\nOn Oct 2, Freshworks wrote:\n"
                      "> account 999988887777\n")
    assert meta.accounts == [{"account": "000123456789"}]
    _, meta = extract("From: a@b.com\n\nInvoice for USD 10\n> IBAN GB82 WEST 1234 5698 7654 32")
    assert meta.accounts == []


@pytest.mark.parametrize("iban,ok", [(MULE, True), ("DE89 3704 0044 0532 0130 00", True),
                                     ("GB33BUKB20201555555555", True), ("GB82 WEST 1234 5698 7654 33", False),
                                     ("INV-2026-1043", False), ("", False)])
def test_iban_valid_from_aditya(iban, ok):
    assert geo.iban_valid(iban) is ok


def test_amount_anomaly_from_aditya(client, graph):
    seed_freshworks(graph)
    v, _ = verify(email(body=ON_FILE).replace("USD 4,200.00", "USD 18,400.00"), client=client, graph=graph, save=False)
    c9 = checks(v)["C9_amount"]
    assert c9.status == "contradicted" and c9.weight == 10


def test_too_little_history_from_aditya(client, graph):
    """C9 needs >= 3 prior payments: with one, even 10x the amount is not an anomaly."""
    seed_freshworks(graph, n=1)
    v, _ = verify(email(body=ON_FILE).replace("USD 4,200.00", "USD 42,000.00"), client=client, graph=graph, save=False)
    assert "C9_amount" not in checks(v)


def test_small_vendor_known_in_graph_from_aditya(client, graph):
    """S2: vendor not in Crustdata but in our payment history -> G1 money confirmed, no risk, PAY under the limit."""
    fp = graph.fingerprint(account="12345678", code="ICIC0001234")
    comp = graph.node("company:domain:tiny-vendor.io", "company", "Tiny Vendor")
    for i in range(3):
        graph.record_payment(f"t{i}", comp, f"TV-2026-00{i}", 100.0, "USD", fp, f"2026-0{i + 1}-01", "test",
                             "tiny-vendor.io")
    raw = ("From: Tiny Vendor Billing <billing@tiny-vendor.io>\nSubject: Invoice TV-2026-009\n\n"
           "Please pay invoice TV-2026-009 for USD 100.00 to IFSC ICIC0001234 account 12345678.\n"
           "Vendor: Tiny Vendor, tiny-vendor.io")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    c = checks(v)
    assert c["C1_vendor"].status == "unchecked"
    assert c["G1_account_history"].status == "verified" and c["G1_account_history"].confirms == "money"
    assert v.risk_score == 0 and v.decision == "PAY"


def test_check_crash_isolated_from_aditya(client, graph, monkeypatch):
    """S10: a crashing PAYMENT check becomes unchecked/api_error and its message doesn't leak into evidence."""
    import payeeguard.pipeline as p
    seed_freshworks(graph)

    def boom(inv, ctx):
        raise RuntimeError("000123456789")
    monkeypatch.setattr(p, "check_payment", boom)
    v, md = verify(email(body=ON_FILE), client=client, graph=graph, save=False)
    row = checks(v)["G_payment"]
    assert row.status == "unchecked" and row.unchecked_reason == "api_error"
    assert all("000123456789" not in e.detail for e in v.evidence) and "000123456789" not in md
    assert v.decision in ("PAY", "HOLD", "BLOCK")


def test_no_full_account_in_evidence_from_aditya(sg):
    """Y2 for evidence rows: only country + last 4 ever appear."""
    for name in ("01_legit.txt", "04_new_account_real_domain.txt", "05_zoho_lookalike_mule.txt",
                 "06_hero_freshworks_mule.txt", "07_unknown_vendor.txt", "09_duplicate_invoice.txt"):
        v = run(sg, name)
        for r in v.evidence:
            for full in ("000123456789", "50200012345678", "GB82WEST", "WEST 1234", "GB33BUKB", "BUKB 2020"):
                assert full not in r.detail and full not in r.source, (name, r.check)


# ============================================================== test_checks_signatory.py

def person(pid, name, current=(), past=(), title="Accounts Receivable Manager", function="Finance",
           start="2021-04-01", end="2024-12-15", other_current=()):
    def job(cid, nm, t, fc, end_date=None):
        return {"crustdata_company_id": cid, "name": nm, "title": t, "function_category": fc,
                "start_date": start, "end_date": end_date}
    cur = [job(c, "Freshworks", title, function) for c in current] + \
          [job(c, nm, "Advisor", "Consulting") for c, nm in other_current]
    return {"crustdata_person_id": pid, "basic_profile": {"name": name, "current_title": title},
            "experience": {"employment_details": {"current": cur,
                                                  "past": [job(c, "Freshworks", title, function, end) for c in past]}}}


def signed(name, sender, title="Finance Manager", body=""):
    return email(f"{name} <{sender}>", body=body + f"\nRegards, {name}, {title}")


NEW_BANK = "Please use our new bank details: IBAN GB82 WEST 1234 5698 7654 32"


def c3_rows(v):
    return [e for e in v.evidence if e.check.startswith("C3")]


def test_current_finance_employee_verified_from_aditya(client, graph, mock):
    mock.people = [person(9001, "Asha Menon", current=[FW])]
    v, _ = verify(signed("Asha Menon", "asha@freshworks.com", body=NEW_BANK), client=client, graph=graph, save=False)
    r = checks(v)["C3_signatory"]
    assert r.status == "verified" and "since 2021-04" in r.detail and "Accounts Receivable Manager" in r.detail
    assert r.source == "crustdata:person/9001#experience.employment_details.current"
    assert v.person_id == 9001 and "C3_role_authority" not in checks(v)


def test_ex_employee_contradicted_from_aditya(client, graph, mock):
    mock.people = [person(9002, "Rohan Verma", current=[], past=[FW])]
    v, _ = verify(signed("Rohan Verma", "rohan@freshworks.com"), client=client, graph=graph, save=False)
    r = checks(v)["C3_ex_employee"]
    assert r.status == "contradicted" and r.weight == 35
    assert "left" in r.detail and "2024-12" in r.detail
    assert r.source.endswith("employment_details.past")


def test_multiple_current_jobs_from_aditya(client, graph, mock):
    """D13: current at the vendor plus elsewhere -> current employee."""
    mock.people = [person(9003, "Neha Kapoor", current=[FW], title="Treasury Analyst",
                          other_current=[(555, "Acme Advisory")])]
    v, _ = verify(signed("Neha Kapoor", "neha@freshworks.com", body=NEW_BANK), client=client, graph=graph, save=False)
    r = checks(v)["C3_signatory"]
    assert r.status == "verified" and "Treasury Analyst" in r.detail


def test_multiple_current_jobs_detail_lists_others_from_aditya(client, graph, mock):
    mock.people = [person(9003, "Neha Kapoor", current=[FW], title="Treasury Analyst",
                          other_current=[(555, "Acme Advisory")])]
    v, _ = verify(signed("Neha Kapoor", "neha@freshworks.com"), client=client, graph=graph, save=False)
    assert "Acme Advisory" in checks(v)["C3_signatory"].detail


def test_same_name_people_title_tiebreak_from_aditya(client, graph, mock):
    """D14: two people with the same name -> verified only when the claimed title picks one."""
    mock.people = [person(9004, "Amit Kumar", current=[FW], title="Billing Manager"),
                   person(9005, "Amit Kumar", current=[FW], title="Software Engineer", function="Engineering")]
    v, _ = verify(signed("Amit Kumar", "amit@freshworks.com", title="Billing Manager"), client=client, graph=graph,
                  save=False)
    r = checks(v)["C3_signatory"]
    assert r.status == "verified" and "Billing Manager" in r.detail
    v, _ = verify(signed("Amit Kumar", "amit@freshworks.com", title="Team Lead"), client=client, graph=graph,
                  save=False)
    r = checks(v)["C3_signatory"]
    assert r.status == "unchecked" and "2 people named Amit Kumar" in r.detail


def test_name_must_match_profile_from_aditya(client, graph, mock):
    mock.people = [person(9001, "Asha Menon", current=[FW])]
    v, _ = verify(signed("Asha Rao", "asha.rao@freshworks.com"), client=client, graph=graph, save=False)
    assert checks(v)["C3_signatory"].status == "unchecked"


def test_role_authority_needs_bank_change_from_aditya(client, graph, mock):
    seed_freshworks(graph)
    mock.people = [person(9006, "Arjun Rao", current=[FW], title="Software Engineer", function="Engineering")]
    v, _ = verify(signed("Arjun Rao", "arjun@freshworks.com", title="Engineer", body=ON_FILE), client=client,
                  graph=graph, save=False)
    assert checks(v)["C3_signatory"].status == "verified" and "C3_role_authority" not in checks(v)


def test_strategic_accounts_is_not_a_payments_role_from_aditya(client, graph, mock):
    """'Strategic Accounts' is sales: no bare 'account' keyword."""
    mock.people = [person(9007, "Priya Nair", current=[FW], title="Strategic Accounts Manager", function="Sales")]
    v, _ = verify(signed("Priya Nair", "priya@freshworks.com", title="Strategic Accounts Manager", body=NEW_BANK),
                  client=client, graph=graph, save=False)
    r = checks(v)["C3_role_authority"]
    assert r.status == "contradicted" and r.weight == 20


def test_finance_title_without_category_from_aditya(client, graph, mock):
    mock.people = [person(9008, "Arjun Rao", current=[FW], title="Accounts Payable Lead", function=None)]
    v, _ = verify(signed("Arjun Rao", "arjun@freshworks.com", body=NEW_BANK), client=client, graph=graph, save=False)
    assert checks(v)["C3_signatory"].status == "verified" and "C3_role_authority" not in checks(v)


def _recording_client(mock, bodies, override=None):
    def handler(req):
        body = json.loads(req.content or b"{}")
        bodies.append((req.url.path, body))
        if override:
            resp = override(req.url.path, body)
            if resp is not None:
                return resp
        return mock(req)
    return CrustdataClient(api_key="test-key", transport=httpx.MockTransport(handler))


def test_c3_identify_403_falls_back_from_aditya(env, graph, mock):
    """D2: Person Identify 403 -> no retry, straight to Person Search by domain + name, limit 5."""
    mock.people = [person(9001, "Asha Menon", current=[FW])]
    bodies = []
    c = _recording_client(mock, bodies)
    v, _ = verify(signed("Asha Menon", "asha@freshworks.com"), client=c, graph=graph, save=False)
    assert checks(v)["C3_signatory"].status == "verified"
    paths = [p for p, _ in bodies]
    assert paths.count("/person/identify") == 1 and paths.count("/person/search") == 1
    body = next(b for p, b in bodies if p == "/person/search")
    fields = [cond["field"] for cond in body["filters"]["conditions"]]
    assert fields == ["experience.employment_details.current.company_website_domain", "basic_profile.name"]
    assert body["filters"]["conditions"][0]["value"] == ["freshworks.com"] and body["limit"] == 5


def test_second_search_finds_ex_employee_from_aditya(env, graph, mock):
    ex = person(9002, "Rohan Verma", current=[], past=[FW])

    def override(path, body):
        if path == "/person/search":
            field = body["filters"]["conditions"][0]["field"]
            found = [ex] if field.endswith("company_name") else []
            return httpx.Response(200, json={"profiles": found, "total_count": len(found)})
    bodies = []
    c = _recording_client(mock, bodies, override)
    v, _ = verify(signed("Rohan Verma", "rohan@freshworks.com"), client=c, graph=graph, save=False)
    assert checks(v)["C3_ex_employee"].weight == 35
    searches = [b for p, b in bodies if p == "/person/search"]
    assert len(searches) == 2
    assert searches[1]["filters"]["conditions"][0] == {"field": "experience.employment_details.company_name",
                                                       "type": "in", "value": ["Freshworks"]}


def _identify_returns(mock, profile):
    mock.overrides["/person/identify"] = (200, [{"matches": [{"confidence_score": 0.95, "person_data": profile}]}])


def test_identify_match_works_elsewhere_from_aditya(client, graph, mock):
    _identify_returns(mock, person(9009, "Karan Mehta", current=[], other_current=[(777, "Other Corp")]))
    v, _ = verify(signed("Karan Mehta", "karan@freshworks.com"), client=client, graph=graph, save=False)
    r = checks(v)["C3_works_elsewhere"]
    assert r.status == "contradicted" and r.weight == 30 and "Other Corp" in r.detail
    assert "/person/search" not in mock.calls


def test_identify_match_current_from_aditya(client, graph, mock):
    _identify_returns(mock, person(9001, "Asha Menon", current=[FW]))
    v, _ = verify(signed("Asha Menon", "asha@freshworks.com"), client=client, graph=graph, save=False)
    assert checks(v)["C3_signatory"].status == "verified"
    assert "identify" in checks(v)["C3_signatory"].source or v.person_id == 9001


def test_gmail_sender_not_found_no_penalty_from_aditya(client, graph, mock):
    """Free mail skips Person Identify; an invented person -> unchecked/no_data, 0 risk."""
    mock.people = []
    raw = signed("Kavya Iyer", "kavya.iyer.finance@gmail.com")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    r = checks(v)["C3_signatory"]
    assert r.status == "unchecked" and r.unchecked_reason == "no_data" and r.weight == 0
    assert "/person/identify" not in mock.calls


def test_vendor_unresolved_makes_no_calls_from_aditya(client, graph, mock):
    mock.people = [person(9001, "Asha Menon", current=[FW])]
    raw = signed("Asha Menon", "asha@unknown-thing.com").replace("Freshworks Inc, freshworks.com",
                                                                 "Unknown Thing Ltd, unknown-thing.com")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    r = checks(v)["C3_signatory"]
    assert r.status == "unchecked" and r.unchecked_reason == "no_data"
    assert "/person/search" not in mock.calls and "/person/identify" not in mock.calls


def test_api_down_is_unchecked_from_aditya(client, graph, mock):
    mock.overrides["/person/search"] = (500, {"error": "down"})
    v, _ = verify(signed("Asha Menon", "asha@freshworks.com"), client=client, graph=graph, save=False)
    r = checks(v)["C3_signatory"]
    assert r.status == "unchecked" and r.unchecked_reason == "api_error" and r.weight == 0


def test_no_sender_name_from_aditya(client, graph):
    """Role mailbox / no personal name -> C3 unchecked, no risk (integrated reason: not_applicable)."""
    v, _ = verify(email(), client=client, graph=graph, save=False)
    r = checks(v)["C3_signatory"]
    assert r.status == "unchecked" and r.weight == 0 and r.unchecked_reason


def test_malformed_responses_from_aditya(client, graph, mock):
    mock.overrides["/person/search"] = (200, {"profiles": None})
    v, _ = verify(signed("Asha Menon", "asha@freshworks.com"), client=client, graph=graph, save=False)
    assert checks(v)["C3_signatory"].unchecked_reason == "no_data"
    mock.overrides["/person/search"] = (200, {"profiles": [{"crustdata_person_id": 1,
                                                            "basic_profile": {"name": "Asha Menon"},
                                                            "experience": None}]})
    v, _ = verify(signed("Asha Menon", "asha@freshworks.com", title="Finance Lead"), client=client, graph=graph,
                  save=False)
    assert checks(v)["C3_signatory"].status == "unchecked"


# ============================================================== test_graph.py

def test_fp_secret_required_whitespace_from_aditya(env, monkeypatch):
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", "   ")
    with pytest.raises(RuntimeError, match="PAYEEGUARD_FP_SECRET"):
        Graph(env / "g.db")


def test_fp_secret_missing_env_from_aditya(env, monkeypatch):
    monkeypatch.delenv("PAYEEGUARD_FP_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="missing"):
        Graph(env / "g.db")


def test_graph_autocreate_from_aditya(env):
    """G-10: nested path created, WAL mode, data survives reopening, plain SQLite tables."""
    path = env / "deep" / "nested" / "graph.db"
    g = Graph(path)
    assert path.exists()
    assert g.db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    g.node("case:x", "case", "x")
    g.commit()
    g.close()
    g2 = Graph(path)
    assert g2.db.execute("SELECT label FROM nodes WHERE id='case:x'").fetchone()[0] == "x"
    g2.close()
    con = sqlite3.connect(path)
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"nodes", "edges",
                                                                                                 "payments"}
    con.close()


def test_locked_database_waits_instead_of_failing_from_aditya(env):
    """G-10: a write while another connection holds the write lock waits instead of failing."""
    g2 = Graph(env / "g.db")
    locked, release = threading.Event(), threading.Event()

    def holder():
        con = sqlite3.connect(env / "g.db")
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO nodes VALUES('n:1','t','l','now')")
        locked.set()
        release.wait(0.3)                     # time.sleep is patched out by the env fixture
        con.commit()
        con.close()
    t = threading.Thread(target=holder)
    t.start()
    locked.wait(5)
    g2.node("n:2", "t", "l")                  # blocks until the holder commits, then succeeds
    g2.commit()
    t.join()
    assert {r[0] for r in g2.db.execute("SELECT id FROM nodes")} >= {"n:1", "n:2"}
    g2.close()


def test_bank_normalisation_from_aditya(graph):
    a = graph.fingerprint(iban="GB82 WEST 1234 5698 7654 32")
    b = graph.fingerprint(iban="gb82west12345698765432")
    c = graph.fingerprint(iban="GB82-WEST-1234-5698-7654-32")
    assert a.id == b.id == c.id and a.masked == "••••5432 GB"
    d = graph.fingerprint(account="0001-2345-6789", code="chasus33")
    e = graph.fingerprint(account="000123456789", code="CHASUS33")
    assert d.id == e.id and d.strong


def test_bank_normalisation_swift_branch_suffix_from_aditya(graph):
    d = graph.fingerprint(account="000123456789", code="CHASUS33XXX")
    e = graph.fingerprint(account="000123456789", code="CHASUS33")
    assert d.id == e.id


def test_fingerprint_is_keyed_from_aditya(env, monkeypatch):
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", "secret-one-0123456789")
    g1 = Graph(env / "a.db")
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", "secret-two-0123456789")
    g2 = Graph(env / "b.db")
    assert g1.fingerprint(iban=MULE).id != g2.fingerprint(iban=MULE).id
    g1.close()
    g2.close()


def test_fingerprint_kinds_from_aditya(graph):
    weak_only = graph.fingerprint(code="CHASUS33", last4="6789")
    assert not weak_only.strong and weak_only.id.startswith("account:weak:")
    strong = graph.fingerprint(account="000123456789", code="CHASUS33")
    assert strong.strong and strong.weak == weak_only.weak         # weak id lets history compare by last 4
    assert graph.fingerprint() is None
    assert graph.fingerprint(last4="6789") is None                 # last 4 without a bank code is nothing


def test_case_id_normalises_whitespace_from_aditya():
    assert case_id_for("a  b\n c") == case_id_for("a b c")


def test_no_full_account_in_outputs_from_aditya(sg, env):
    """Y2: the database never holds a raw account number, IBAN or sender address."""
    c, g = sg
    run(sg, "05_zoho_lookalike_mule.txt")
    run(sg, "06_hero_freshworks_mule.txt")
    g.commit()
    blob = b"".join(p.read_bytes() for p in env.glob("eval_graph.db*"))
    for secret in (b"000123456789", b"50200012345678", b"GB82WEST12345698765432", b"WEST 1234 5698",
                   b"billing@freshworks.com", b"ananya.rao@zoh0-billing.com"):
        assert secret not in blob


def test_seed_loads_and_is_idempotent_from_aditya(sg):
    c, g = sg
    seed(g, c)                                                     # second load writes nothing new
    assert len(g.payments_for(FWN)) == 14 and len(g.payments_for("company:1032449")) == 6
    assert g.history_domains(FWN) == {"freshworks.com"}
    cites = {r[0] for r in g.db.execute("SELECT citation FROM edges WHERE rel IN ('PAID','PAID_FROM_DOMAIN')")}
    assert cites == {"seed:samples/seed_history.json"}


def test_seed_offline_message_from_aditya(env, graph, monkeypatch):
    monkeypatch.setenv("PAYEEGUARD_OFFLINE", "1")                   # env fixture's cache dir is empty
    with pytest.raises(Exception, match=r"freshworks\.com.*(cache|offline)"):
        seed(graph, CrustdataClient(api_key="k"))


def test_query_excludes_own_case_from_aditya(graph):
    """G-1: every query excludes the current case."""
    fp = graph.fingerprint(iban=MULE)
    graph.account(fp)
    graph.flag("case05", [fp.id], "test")
    assert graph.cases_flagged(fp.id, exclude_case="case05") == []
    assert len(graph.cases_flagged(fp.id, exclude_case="case06")) == 1
    graph.record_payment("caseP", FWN, "X-1", 10, "USD", fp, "2026-10-01", "test", "freshworks.com")
    assert fp.id not in graph.accounts_paid(FWN, exclude_case="caseP")
    assert graph.invoice_paid(FWN, "X-1", exclude_case="caseP") is None
    assert graph.invoice_paid(FWN, "X-1", exclude_case="other") is not None


def test_verify_idempotent_from_aditya(sg):
    """G-2: verify writes only SENT / CLAIMS_VENDOR / PAYS_TO for a HOLD, idempotently; never PAID."""
    c, g = sg
    a = run(sg, "04_new_account_real_domain.txt")
    n_edges = g.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    b = run(sg, "04_new_account_real_domain.txt")
    assert (a.decision, a.risk_score, a.decision_rule) == (b.decision, b.risk_score, b.decision_rule)
    assert g.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == n_edges
    rels = {r[0] for r in g.db.execute("SELECT rel FROM edges WHERE case_id=?", (a.case_id,))}
    assert rels == {"SENT", "CLAIMS_VENDOR", "PAYS_TO"}


def test_override_revokes_flags_reblock_stays_revoked_from_aditya(client, graph):
    """G-7: after an override, re-running the same BLOCK text must not un-revoke its FLAGGED_IN edges."""
    raw = email("Bad <bad@freshw0rks-pay.com>", body=f"New bank details: {HERO}")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    fp = graph.fingerprint(iban=MULE)
    assert graph.cases_flagged(fp.id, "other")
    gate.resolve_case(v.case_id, "override", "Asha", "vendor confirmed on call", graph)
    assert graph.cases_flagged(fp.id, "other") == []
    verify(raw, client=client, graph=graph, save=False)
    assert graph.cases_flagged(fp.id, "other") == []


def test_block_does_not_flag_real_domain_by_default_from_aditya(sg):
    run(sg, "05_zoho_lookalike_mule.txt")
    v6 = run(sg, "06_hero_freshworks_mule.txt")
    assert v6.decision == "BLOCK"
    assert sg[1].cases_flagged("domain:freshworks.com") == []


def test_approved_bank_change_from_aditya(client, graph, outbox):
    """G-8: an approved new account joins the history; future invoices to either account verify."""
    seed_freshworks(graph)
    v, _ = verify(email(body=f"Please remit to our UK entity {HERO}"), client=client, graph=graph, save=False)
    gate.resolve_case(v.case_id, "approve", "Asha", "called vendor on official number", graph)
    assert "SIMULATED" in gate.pay_invoice(v.case_id, graph)
    new, _ = verify(email(body=f"Pay {HERO}").replace("1043", "1201"), client=client, graph=graph, save=False)
    old, _ = verify(email(body=ON_FILE).replace("1043", "1202"), client=client, graph=graph, save=False)
    assert checks(new)["G1_account_history"].status == "verified"
    assert checks(old)["G1_account_history"].status == "verified"


def test_money_trail_hero_from_aditya(sg):
    run(sg, "05_zoho_lookalike_mule.txt")
    v6 = run(sg, "06_hero_freshworks_mule.txt")
    rels = [e["rel"] for e in v6.graph_edges]
    assert {"CLAIMS_VENDOR", "PAYS_TO", "FLAGGED_IN"} <= set(rels)
    paid = next(e for e in v6.graph_edges if e["rel"].startswith("PAID"))
    assert paid["rel"] == "PAID ×14" and "6789" in paid["dst_label"]
    acct = next(e for e in v6.graph_edges if e["rel"] == "PAYS_TO")
    assert acct["state"] == "bad" and "5432" in acct["dst_label"]
