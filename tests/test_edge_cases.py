"""Section 13 edge-case matrix. Every test names its row (E*, H*, D*, G-*, S*, P*, M*, Y*)."""

import json

import httpx
import pytest

from conftest import FW, email, seed_freshworks
from payeeguard import audit, gate, geo
from payeeguard.checks_channel import lookalike_reason
from payeeguard.client import BadRequest, CrustdataClient, NotEnabled, OfflineMiss
from payeeguard.extract import cross_check, extract, parse_number
from payeeguard.graph import Graph
from payeeguard.models import Evidence
from payeeguard.pipeline import verify
from payeeguard.scoring import score


def checks(v):
    return {e.check: e for e in v.evidence}


# ---- 13.1 input and extraction ----------------------------------------------------------------------

def test_E1_empty(client, graph):
    v, md = verify("   ", client=client, graph=graph, save=False)
    assert v.decision == "HOLD" and v.confidence == "LOW" and "No content" in v.summary


def test_E2_not_payment_request(client, graph, mock):
    v, _ = verify("From: news@site.com\nSubject: Weekly digest\n\nHello, here are this week's articles.",
                  client=client, graph=graph, save=False)
    assert v.decision == "HOLD" and "No payment request" in v.summary and mock.calls == []


def test_E3_forwarded_sender():
    raw = ("From: Me <me@buyer.com>\nSubject: Fwd: invoice\n\n---------- Forwarded message ---------\n"
           "From: Bad Guy <bad@freshw0rks-billing.com>\nSubject: pay\n\nPay USD 100 to IBAN GB82 WEST 1234 5698 7654 32")
    inv, meta = extract(raw)
    assert inv.sender_email == "bad@freshw0rks-billing.com" and meta.forwarded


def test_E4_quoted_history_ignored():
    raw = "From: a@freshworks.com\n\nPay USD 100.\n> old: pay USD 99999 to IBAN DE89 3704 0044 0532 0130 00\n"
    inv, meta = extract(raw)
    assert inv.amount == 100 and not inv.bank_iban


def test_E5_html_input():
    inv, _ = extract("<html><body><p>From: Billing &lt;billing@freshworks.com&gt;</p><p>Pay USD 1,250.50 for "
                     "invoice INV-2026-1043</p></body></html>")
    assert inv.amount == 1250.50


@pytest.mark.parametrize("text,value,cur", [
    ("USD 4,200.00", 4200.0, "USD"), ("$4,200", 4200.0, "USD"), ("₹3,45,000", 345000.0, "INR"),
    ("Rs. 3,45,000/-", 345000.0, "INR"), ("INR 3.45 lakh", 345000.0, "INR"), ("EUR 9.800,00", 9800.0, "EUR"),
    ("9 800,00 €", 9800.0, "EUR"), ("£1,000", 1000.0, "GBP"),
])
def test_E6_amount_formats(text, value, cur):
    inv, _ = extract(f"From: a@b.com\n\nInvoice total {text} due.")
    assert inv.amount == pytest.approx(value) and inv.currency == cur


def test_E6_parse_number_edge():
    assert parse_number("1.234.567,89") == pytest.approx(1234567.89)
    assert parse_number("2 crore") == 20_000_000


def test_E7_amount_missing_holds(client, graph):
    seed_freshworks(graph)
    raw = ("From: Freshworks Billing <billing@freshworks.com>\nSubject: Invoice INV-2026-1043\n\n"
           "Please pay invoice INV-2026-1043 to account on file. Vendor: Freshworks Inc, freshworks.com")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert v.decision == "HOLD" and "amount" in v.decision_rule


def test_E8_amount_conflict(client, graph):
    raw = email(body="Total USD 4,200.00\nAmount due USD 5,100.00")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert checks(v)["X2_amount_conflict"].status == "contradicted"


def test_E10_multiple_accounts_hold(client, graph):
    seed_freshworks(graph)
    raw = email(body="Pay IBAN GB82 WEST 1234 5698 7654 32 or alternatively IBAN DE89 3704 0044 0532 0130 00")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert checks(v)["G5_ambiguous_payment"].status == "contradicted" and v.decision != "PAY"


def test_E11_account_on_file(client, graph):
    seed_freshworks(graph)
    v, _ = verify(email(body="Pay to our existing account on file."), client=client, graph=graph, save=False)
    g1 = checks(v)["G1_account_history"]
    assert g1.status == "verified" and g1.confirms == "money" and v.decision == "PAY"


def test_E12_bank_normalisation(graph):
    a = graph.fingerprint(iban="gb82 west 1234 5698 7654 32")
    b = graph.fingerprint(iban="GB82WEST12345698765432")
    assert a.id == b.id and a.strong


def test_E13_email_normalisation():
    inv, _ = extract("From: Billing <BILLING+inv@FreshWorks.com.>\n\nPay USD 10 invoice INV-2026-1")
    assert inv.sender_email == "billing@freshworks.com"
    inv, _ = extract("From: billing [at] freshworks [dot] com\n\nPay USD 10 invoice")
    assert inv.sender_email == "billing@freshworks.com"


def test_E14_hinglish_urgency():
    inv, _ = extract("From: x@y.com\n\nInvoice ka payment turant karo, USD 100")
    assert inv.urgency_language


def test_E15_huge_input(client, graph):
    v, _ = verify(email(body="x " * 50_000), client=client, graph=graph, save=False)
    assert v.decision in ("PAY", "HOLD", "BLOCK")


def test_E16_field_truncation():
    inv, _ = extract(email())
    inv, _ = cross_check(inv, {"sender_title": "A" * 5000})
    assert len(inv.sender_title) <= 200


# ---- 13.2 email channel -----------------------------------------------------------------------------

def test_H1_subdomain_verified(client, graph):
    v, _ = verify(email("Billing <billing@billing.freshworks.com>"), client=client, graph=graph, save=False)
    assert checks(v)["C2_domain"].status == "verified"


def test_H2_multipart_tld():
    assert geo.registrable_domain("mail.acme.co.in") == "acme.co.in"
    assert geo.registrable_domain("x.acme.co.uk") == "acme.co.uk"


@pytest.mark.parametrize("host", ["freshworks.com.secure-pay.ru", "freshworks.billing-portal.net"])
def test_H3_brand_in_subdomain(host):
    assert lookalike_reason(host, "freshworks.com")


def test_H4_display_name_spoof(client, graph):
    v, _ = verify(email('"billing@freshworks.com" <pay@payments-desk.net>'), client=client, graph=graph, save=False)
    assert checks(v)["C2c_display_name"].status == "contradicted"


def test_H5_sibling_domain(client, graph, mock):
    from conftest import FRESHWORKS
    mock.extra_domains["freshdesk.com"] = FRESHWORKS
    v, _ = verify(email("Billing <billing@freshdesk.com>"), client=client, graph=graph, save=False)
    assert checks(v)["C2_domain"].status == "verified"


def test_H6_short_label_not_lookalike():
    assert lookalike_reason("hq.com", "hp.com") is None


def test_H7_idn_homoglyph():
    assert lookalike_reason("frеshworks.com", "freshworks.com")      # Cyrillic е
    assert lookalike_reason("xn--frshworks-6ig.com", "freshworks.com") or \
        lookalike_reason("frеshworks.com".encode("idna").decode(), "freshworks.com")


@pytest.mark.parametrize("host", ["freshw0rks.com", "freshworks-billing.com", "freshworks.co", "freshvvorks.com"])
def test_H_lookalike_variants(host):
    assert lookalike_reason(host, "freshworks.com")


def test_H8_processor_allowed(client, graph):
    v, _ = verify(email("Freshworks via Stripe <invoices@stripe.com>"), client=client, graph=graph, save=False)
    assert checks(v)["C2_domain"].status == "verified"


def test_H9_nonexistent_domain(client, graph, mock):
    mock.dns_status = 3
    v, _ = verify(email("Accounts <a@totally-new-vendor.io>").replace("Freshworks Inc, freshworks.com", "Newco"),
                  client=client, graph=graph, save=False)
    assert checks(v)["C8_domain_age"].status == "contradicted"


def test_H10_rdap_unavailable(client, graph, mock):
    mock.rdap = (404, None)
    v, _ = verify(email("Accounts <a@some-vendor.io>").replace("Freshworks Inc, freshworks.com", "Newco"),
                  client=client, graph=graph, save=False)
    c8 = [e for e in v.evidence if e.check == "C8_domain_age"][0]
    assert c8.status == "unchecked"


def test_H11_no_sender(client, graph):
    v, _ = verify("Invoice INV-2026-1043 for USD 4,200. Vendor: Freshworks Inc, freshworks.com",
                  client=client, graph=graph, save=False)
    assert checks(v)["C2_domain"].unchecked_reason == "not_applicable" and v.decision != "PAY"


# ---- 13.3 Crustdata client and data -----------------------------------------------------------------

def _client(env, handler):
    return CrustdataClient(api_key="k", transport=httpx.MockTransport(handler))


def test_D1_429_backoff_then_success(env):
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(429) if n["i"] < 3 else httpx.Response(200, json=[{"matches": []}])
    assert _client(env, h).post("/company/identify", {"domains": ["a.com"]}) == [{"matches": []}]


def test_D2_403_cached(env):
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(403, json={})
    c = _client(env, h)
    for _ in range(3):
        with pytest.raises(NotEnabled):
            c.post("/person/identify", {"emails": ["a@b.com"]})
    assert n["i"] == 1


def test_D3_5xx_retry_then_error(env):
    c = _client(env, lambda r: httpx.Response(503))
    with pytest.raises(Exception):
        c.post("/company/identify", {"domains": ["a.com"]})


def test_D3_bad_json(env):
    c = _client(env, lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(Exception, match="malformed"):
        c.post("/company/identify", {"domains": ["a.com"]})


def test_D4_missing_key_fail_safe(env, graph, monkeypatch):
    c = CrustdataClient(api_key="", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    v, md = verify(email(), client=c, graph=graph, save=False)
    assert v.decision == "HOLD" and "Crustdata" in md


def test_D400_invalid_domain_is_no_match(env):
    c = _client(env, lambda r: httpx.Response(400, json={"error": {"message": "Invalid company domains"}}))
    with pytest.raises(BadRequest):
        c.post("/company/identify", {"domains": ["x.example"]})


def test_D_offline_miss(env, monkeypatch):
    monkeypatch.setenv("PAYEEGUARD_OFFLINE", "1")
    with pytest.raises(OfflineMiss):
        _client(env, lambda r: httpx.Response(200, json=[])).post("/company/identify", {"domains": ["a.com"]})


def test_D6_no_match_unchecked(client, graph):
    raw = email().replace("Freshworks Inc, freshworks.com", "Unknown Thing Ltd, unknown-thing.com")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert checks(v)["C1_vendor"].status == "unchecked"


def test_D11_geo_normalise():
    assert geo.to_iso2("USA") == "US" and geo.office_country("79 Hatton Garden, London, EC1N 8JS, GB") == "GB"


def _person(pid, current, past=(), title="Accounts Receivable Analyst", function="Finance"):
    job = lambda cid, end=None: {"crustdata_company_id": cid, "name": "Co", "title": title,  # noqa: E731
                                 "function_category": function, "start_date": "2023-01-01", "end_date": end}
    return {"crustdata_person_id": pid, "basic_profile": {"name": "Kavya Iyer", "current_title": title},
            "experience": {"employment_details": {"current": [job(c) for c in current],
                                                  "past": [job(c, "2024-12-01") for c in past]}}}


def _signed(name="Kavya Iyer", sender="kavya@freshworks.com", body=""):
    return email(f"{name} <{sender}>", body=body + f"\nRegards, {name}, Finance Manager")


def test_D12_promotion_not_ex_employee(client, graph, mock):
    mock.people = [_person(7, current=[FW], past=[FW])]
    v, _ = verify(_signed(), client=client, graph=graph, save=False)
    assert checks(v)["C3_signatory"].status == "verified" and "C3_ex_employee" not in checks(v)


def test_D12b_ex_employee(client, graph, mock):
    mock.people = [_person(7, current=[999], past=[FW])]
    v, _ = verify(_signed(), client=client, graph=graph, save=False)
    assert checks(v)["C3_ex_employee"].weight == 35


def test_D14_same_name_people(client, graph, mock):
    mock.people = [_person(7, current=[FW], title="Engineer"), _person(8, current=[FW], title="Designer")]
    v, _ = verify(_signed(), client=client, graph=graph, save=False)
    assert checks(v)["C3_signatory"].status == "unchecked"


def test_C3_role_authority(client, graph, mock):
    mock.people = [_person(7, current=[FW], title="Software Engineer", function="Engineering")]
    v, _ = verify(_signed(body="Please use our new bank details: IBAN GB82 WEST 1234 5698 7654 32"),
                  client=client, graph=graph, save=False)
    assert checks(v)["C3_role_authority"].status == "contradicted"


def test_C3_not_found_adds_no_risk(client, graph, mock):
    mock.people = []
    v, _ = verify(_signed(), client=client, graph=graph, save=False)
    assert checks(v)["C3_signatory"].status == "unchecked" and checks(v)["C3_signatory"].weight == 0


# ---- 13.4 graph and memory --------------------------------------------------------------------------

HERO = "IBAN GB82 WEST 1234 5698 7654 32 SWIFT WESTGB2L"


def test_G1_case_does_not_match_itself(client, graph):
    raw = email("Bad <bad@freshw0rks-pay.com>", body=f"New bank details: {HERO}")
    v1, _ = verify(raw, client=client, graph=graph, save=False)
    v2, _ = verify(raw, client=client, graph=graph, save=False)
    assert v1.decision == v2.decision == "BLOCK"
    assert "G3_account_flagged" not in checks(v2)


def test_G2_verify_idempotent(client, graph):
    seed_freshworks(graph)
    raw = email(body="Pay to account on file: Chase SWIFT CHASUS33 account 000123456789")
    a, _ = verify(raw, client=client, graph=graph, save=False)
    b, _ = verify(raw, client=client, graph=graph, save=False)
    assert (a.decision, a.risk_score, a.decision_rule) == (b.decision, b.risk_score, b.decision_rule)


def test_G3_no_double_pay(client, graph):
    seed_freshworks(graph)
    v, _ = verify(email(body="Pay to account on file: Chase SWIFT CHASUS33 account 000123456789"),
                  client=client, graph=graph, save=False)
    assert v.decision == "PAY"
    assert "SIMULATED payment" in gate.pay_invoice(v.case_id, graph)
    assert "already paid" in gate.pay_invoice(v.case_id, graph)


def test_mule_campaign_hero(client, graph):
    seed_freshworks(graph)
    verify(email("Ana <ana@freshw0rks-billing.com>", body=f"New account {HERO}"), client=client, graph=graph, save=False)
    v, _ = verify(email(body=f"Please remit to our UK entity {HERO}").replace("1043", "1102"),
                  client=client, graph=graph, save=False)
    assert v.decision == "BLOCK" and v.hypothesis.attack_type == "mule_campaign"
    assert any(r.attack_type == "fake_vendor" for r in v.ruled_out)


def test_G5_weak_fp_never_hard(client, graph):
    seed_freshworks(graph)
    fp = graph.fingerprint(code="WESTGB2L", last4="5432")
    assert not fp.strong
    graph.flag("old", [fp.id], "test")
    v, _ = verify(email(body="Pay to SWIFT WESTGB2L account ending 5432"), client=client, graph=graph, save=False)
    assert "G3_account_flagged" not in checks(v)


def test_G6_shared_account_hold_not_block(client, graph):
    seed_freshworks(graph)
    fp = graph.fingerprint(iban="GB82WEST12345698765432")
    graph.record_payment("other", "company:123", "X-1", 10.0, "USD", fp, "2026-01-01", "test", "other.com")
    v, _ = verify(email(body=f"Pay {HERO}"), client=client, graph=graph, save=False)
    assert checks(v)["G2_account_other_vendor"].weight == 30 and v.decision != "BLOCK"


def test_G7_override_revokes_flags(client, graph):
    raw = email("Bad <bad@freshw0rks-pay.com>", body=f"New bank details: {HERO}")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    assert "overridden by Asha" in gate.resolve_case(v.case_id, "override", "Asha", "vendor confirmed by phone", graph)
    fp = graph.fingerprint(iban="GB82WEST12345698765432")
    assert graph.cases_flagged(fp.id) == []


def test_G11_fp_secret_required(env, monkeypatch):
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", "")
    with pytest.raises(RuntimeError, match="FP_SECRET"):
        Graph(env / "x.db")


# ---- 13.5 decisions ---------------------------------------------------------------------------------

def test_S1_api_down_holds(env, graph, monkeypatch):
    seed_freshworks(graph)
    monkeypatch.setenv("PAYEEGUARD_OFFLINE", "1")
    c = CrustdataClient(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    v, _ = verify(email(body="Pay to account on file"), client=c, graph=graph, save=False)
    assert v.decision == "HOLD" and "unavailable" in v.decision_rule


def test_S2_small_known_vendor_pays_under_limit():
    ev = [Evidence("C2_domain", "CHANNEL", "", "verified", "", "", confirms="channel"),
          Evidence("G1_account_history", "PAYMENT", "", "verified", "", "", confirms="money")]
    s = score(ev, vendor_known=False, amount=100, currency="USD", bank_change=False)
    assert (s.confidence, s.decision) == ("MEDIUM", "PAY")
    s = score(ev, vendor_known=False, amount=999_999, currency="USD", bank_change=False)
    assert s.decision == "HOLD"


def test_S3_unknown_vendor_holds_not_blocks():
    ev = [Evidence("C1_vendor", "COMPANY", "", "unchecked", "", "", unchecked_reason="no_data")]
    s = score(ev, vendor_known=False, amount=100, currency="INR", bank_change=True)
    assert s.decision == "HOLD"


def test_S4_no_double_count():
    ev = [Evidence("C2b_freemail", "CHANNEL", "", "contradicted", "", "", 25),
          Evidence("C2c_display_name", "CHANNEL", "", "contradicted", "", "", 30),
          Evidence("C14_channel_switch", "BEHAVIOUR", "", "contradicted", "", "", 5),
          Evidence("C14_secrecy", "BEHAVIOUR", "", "contradicted", "", "", 5)]
    assert score(ev, vendor_known=False, amount=1, currency="USD", bank_change=False).risk == 40


def test_S9_threshold_boundaries():
    ev = [Evidence("C9_amount", "PAYMENT", "", "contradicted", "", "", 25)]
    assert score(ev, vendor_known=True, amount=1, currency="USD", bank_change=False).decision == "HOLD"
    ev = [Evidence("C2c", "CHANNEL", "", "contradicted", "", "", 30),
          Evidence("C3_works_elsewhere", "SIGNATORY", "", "contradicted", "", "", 30)]
    assert score(ev, vendor_known=True, amount=1, currency="USD", bank_change=False).decision == "BLOCK"


def test_S10_check_crash_isolated(client, graph, monkeypatch):
    import payeeguard.pipeline as p
    monkeypatch.setattr(p, "check_c4", lambda inv, ctx: 1 / 0)
    v, _ = verify(email(), client=client, graph=graph, save=False)
    assert v.decision in ("PAY", "HOLD", "BLOCK")


def test_S11_deterministic(client, graph):
    a = verify(email(), client=client, graph=graph, save=False)[0]
    b = verify(email(), client=client, graph=graph, save=False)[0]
    assert [(e.check, e.status) for e in a.evidence] == [(e.check, e.status) for e in b.evidence]


# ---- 13.6 gate and humans ---------------------------------------------------------------------------

def test_P1_unknown_case(graph):
    assert "Unknown case" in gate.pay_invoice("nope", graph)


def test_P2_gate_refuses_block(client, graph):
    v, _ = verify(email("Bad <bad@freshw0rks-pay.com>", body=f"New bank details: {HERO}"),
                  client=client, graph=graph, save=False)
    assert gate.pay_invoice(v.case_id, graph).startswith("Refused")


def test_P2b_hold_needs_approval(client, graph):
    seed_freshworks(graph)
    v, _ = verify(email(body=f"Please remit to our UK entity {HERO}"), client=client, graph=graph, save=False)
    assert v.decision == "HOLD" and gate.pay_invoice(v.case_id, graph).startswith("Refused")
    gate.resolve_case(v.case_id, "approve", "Asha", "called vendor on official number", graph)
    assert "SIMULATED" in gate.pay_invoice(v.case_id, graph)
    # Approval taught the graph: the new account is now in this vendor's history.
    fp = graph.fingerprint(iban="GB82WEST12345698765432")
    assert fp.id in graph.accounts_paid(f"company:{FW}")


def test_P5_dual_approval_distinct(client, graph):
    seed_freshworks(graph)
    raw = email(body=f"Please remit to our UK entity {HERO}").replace("USD 4,200.00", "USD 90,000.00")
    v, _ = verify(raw, client=client, graph=graph, save=False)
    gate.resolve_case(v.case_id, "approve", "Asha", "ok", graph)
    assert "already recorded" in gate.resolve_case(v.case_id, "approve", "Asha", "again", graph)
    assert "dual-approval" in gate.pay_invoice(v.case_id, graph)
    gate.resolve_case(v.case_id, "approve", "Ravi", "second approver", graph)
    assert "SIMULATED" in gate.pay_invoice(v.case_id, graph)


def test_P6_audit_tamper_detected(env):
    audit.append("a", x=1)
    audit.append("b", x=2)
    p = env / "reports" / "audit.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"x":1', '"x":9')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, msg = audit.verify()
    assert not ok and "line 1" in msg


# ---- 13.7 MCP / runtime, 13.8 privacy ----------------------------------------------------------------

def test_M2_prompt_injection_fields(client, graph):
    raw = email("Accounts <accounts@freshworks-invoices.com>")
    v, md = verify(raw, {"sender_email": "billing@freshworks.com"}, client=client, graph=graph, save=False)
    assert checks(v)["X1_extraction"].status == "contradicted" and v.decision == "BLOCK"


def test_Y1_Y2_no_raw_text_or_full_account_saved(client, graph, env):
    v, md = verify(email(body=f"Pay {HERO}"), client=client, graph=graph, save=True)
    blob = "".join(p.read_text(encoding="utf-8") for p in (env / "reports" / v.case_id).glob("*"))
    assert "GB82WEST12345698765432" not in blob and "GB82 WEST 1234" not in blob
    assert "Please pay invoice" not in json.loads((env / "reports" / v.case_id / "report.json").read_text("utf-8")).get(
        "invoice", {}).get("raw_text", "")
    assert "GB82WEST12345698765432" not in md


def test_Y4_artifacts_labelled(client, graph):
    v, _ = verify(email("Bad <bad@freshw0rks-pay.com>", body=f"New bank details: {HERO}"),
                  client=client, graph=graph, save=False)
    assert v.actions and all("SIMULATED / DRAFT" in a.body for a in v.actions)
