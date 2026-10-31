"""C3 signatory timeline + role authority, and the P1 insider check (spec 7, 13.3 D2/D12-D14)."""
from __future__ import annotations

import pytest

from conftest import FRESHWORKS, FakeClient, NotEnabled, OfflineMiss, inv, person, sample, search_returning
from payeeguard.checks_payment import policy
from payeeguard.checks_signatory import check_c3_signatory, check_signatory

COMPANY = {"basic_info": {"name": "Freshworks", "primary_domain": "freshworks.com",
                          "all_domains": ["freshworks.com"]}}


def ctx(**kw):
    return {"company_id": FRESHWORKS, "company": COMPANY, "case_id": "c3case", **kw}


def client_with(*profiles, identify=None):
    identify = identify if identify is not None else NotEnabled("403 permission_error")
    return FakeClient({"/person/identify": identify, "/person/search": search_returning(*profiles)})


def c3(invoice, client, context=None, graph=None):
    rows = check_c3_signatory(invoice, context or ctx(), client, graph)
    assert len(rows) == 1
    r = rows[0]
    assert r.family == "SIGNATORY" and r.source and r.detail
    if r.status != "contradicted":
        assert r.weight == 0
    if r.status == "unchecked":
        assert r.unchecked_reason
    return r


def sender(name, email="billing@freshworks.com", title=None, bank_change=False):
    return inv(vendor_name="Freshworks Inc", vendor_domain="freshworks.com", sender_email=email, sender_name=name,
               sender_title=title, bank_change_requested=bank_change, raw_text=f"from {name}")


# ---------------------------------------------------------------- timeline

def test_current_finance_employee_verified(graph):
    r = c3(sender("Asha Menon", bank_change=True), client_with(person("finance_current")), graph=graph)
    assert r.status == "verified" and "since 2021-04-01" in r.detail and "Accounts Receivable Manager" in r.detail
    assert r.source == "crustdata:person/9001#experience.employment_details.current"
    assert r.confirms is None                                   # C1 already confirms "who"
    assert [e["rel"] for e in graph.edges_for_case("c3case")] == ["EMPLOYED_AT_CURRENT"]


def test_confirms_who_only_without_c1():
    r = c3(sender("Asha Menon"), client_with(person("finance_current")), ctx(c1_verified=False))
    assert r.confirms == "who"


def test_promotion_not_ex_employee():
    """D12: Freshworks in past[] and current[] (promotion) = current employee."""
    r = c3(sender("Vikram Shah", bank_change=True), client_with(person("promotion")))
    assert r.status == "verified" and "Finance Director" in r.detail


def test_ex_employee_contradicted(graph):
    r = c3(sender("Rohan Verma"), client_with(person("ex_employee")), graph=graph)
    assert r.status == "contradicted" and r.weight == 35
    assert "ex-employee" in r.detail and "2024-12" in r.detail
    assert r.source.endswith("employment_details.past")
    assert graph.edges_for_case("c3case")[0]["rel"] == "EMPLOYED_AT_PAST"


def test_multiple_current_jobs():
    """D13: current at the vendor plus elsewhere -> current; the detail lists the others."""
    r = c3(sender("Neha Kapoor", bank_change=True), client_with(person("multi_current")))
    assert r.status == "verified" and "Treasury Analyst" in r.detail and "Acme Advisory" in r.detail


def test_same_name_people():
    """D14: two people with the same name -> unchecked unless the claimed title picks one."""
    cl = client_with(person("same_name_a"), person("same_name_b"))
    r = c3(sender("Amit Kumar"), cl)
    assert r.status == "unchecked" and "2 people named Amit Kumar" in r.detail
    r = c3(sender("Amit Kumar", title="Billing Manager"), client_with(person("same_name_a"), person("same_name_b")))
    assert r.status == "verified" and "Billing Manager" in r.detail
    r = c3(sender("Amit Kumar", title="Manager"), client_with(person("same_name_a"), person("same_name_b")))
    assert r.status == "verified"                               # only one of them is a manager
    r = c3(sender("Amit Kumar", title="Team Lead"), client_with(person("same_name_a"), person("same_name_b")))
    assert r.status == "unchecked"


def test_name_must_match_profile():
    """Word-match search can return near names; only full-name matches count."""
    r = c3(sender("Asha Rao"), client_with(person("finance_current")))
    assert r.status == "unchecked" and r.unchecked_reason == "no_data"


# ---------------------------------------------------------------- role authority

def test_role_authority_with_bank_change():
    r = c3(sender("Arjun Rao", bank_change=True), client_with(person("engineer")))
    assert r.status == "contradicted" and r.weight == 20 and "role authority" in r.detail


def test_role_authority_needs_bank_change():
    r = c3(sender("Arjun Rao"), client_with(person("engineer")))
    assert r.status == "verified"


def test_strategic_accounts_is_not_a_payments_role():
    """'Strategic Accounts' is sales: no bare 'account' keyword."""
    r = c3(sender("Priya Nair", bank_change=True), client_with(person("sales_accounts")))
    assert r.status == "contradicted" and r.weight == 20


def test_finance_title_without_category():
    p = person("engineer")
    p["experience"]["employment_details"]["current"][0].update(title="Accounts Payable Lead", function_category=None)
    r = c3(sender("Arjun Rao", bank_change=True), client_with(p))
    assert r.status == "verified"


# ---------------------------------------------------------------- lookup order and failures

def test_c3_identify_403_falls_back():
    """D2: Person Identify 403 -> no retry, straight to Person Search."""
    cl = client_with(person("finance_current"))
    r = c3(sender("Asha Menon"), cl)
    assert r.status == "verified"
    assert cl.count("/person/identify") == 1 and cl.count("/person/search") == 1
    body = cl.calls[1][1]
    fields = [c["field"] for c in body["filters"]["conditions"]]
    assert fields == ["experience.employment_details.current.company_website_domain", "basic_profile.name"]
    assert body["filters"]["conditions"][0]["value"] == ["freshworks.com"] and body["limit"] == 5


def test_second_search_finds_ex_employee():
    calls = []

    def search(body):
        calls.append(body)
        field = body["filters"]["conditions"][0]["field"]
        found = [person("ex_employee")] if field.endswith("company_name") else []
        return {"profiles": found, "total_count": len(found)}
    cl = FakeClient({"/person/identify": NotEnabled("403"), "/person/search": search})
    r = c3(sender("Rohan Verma"), cl)
    assert r.weight == 35 and len(calls) == 2
    assert calls[1]["filters"]["conditions"][0] == {"field": "experience.employment_details.company_name",
                                                    "type": "in", "value": ["Freshworks"]}


def test_identify_match_works_elsewhere():
    ident = [{"matches": [{"confidence_score": 0.95, "person_data": person("elsewhere")}]}]
    cl = FakeClient({"/person/identify": ident, "/person/search": search_returning()})
    r = c3(sender("Karan Mehta", email="karan@freshworks.com"), cl)
    assert r.status == "contradicted" and r.weight == 30 and "Other Corp" in r.detail
    assert cl.count("/person/search") == 0


def test_identify_match_current():
    ident = [{"matches": [{"confidence_score": 0.95, "person_data": person("finance_current")}]}]
    r = c3(sender("A. Menon"), FakeClient({"/person/identify": ident}))
    assert r.status == "verified"


def test_gmail_sender_not_found_no_penalty():
    """Sample 03: free mail skips Identify; invented person -> unchecked/no_data, 0 risk."""
    i, context = sample("03_gmail_signatory.txt")
    cl = client_with()
    r = c3(i, cl, dict(context, company=COMPANY))
    assert r.status == "unchecked" and r.unchecked_reason == "no_data" and r.weight == 0
    assert cl.count("/person/identify") == 0
    assert "total_count=0" in r.source


def test_vendor_unresolved_makes_no_calls():
    cl = client_with(person("finance_current"))
    r = c3(sender("Asha Menon"), cl, {"case_id": "x"})
    assert r.status == "unchecked" and r.unchecked_reason == "no_data" and cl.calls == []


@pytest.mark.parametrize("exc,reason", [(OfflineMiss("miss"), "api_error"), (NotEnabled("403"), "not_enabled"),
                                        (TimeoutError("t"), "api_error")])
def test_api_down_is_unchecked(exc, reason):
    cl = FakeClient({"/person/identify": exc, "/person/search": exc})
    r = c3(sender("Asha Menon"), cl)
    assert r.status == "unchecked" and r.unchecked_reason == reason


def test_no_sender_name():
    r = c3(sender(None, email="kavya@gmail.com"), client_with())
    assert r.status == "unchecked" and r.unchecked_reason == "no_data"


def test_no_client():
    r = c3(sender("Asha Menon"), None)
    assert r.status == "unchecked"


def test_malformed_responses():
    cl = FakeClient({"/person/identify": [{"matches": None}, "junk"], "/person/search": {"profiles": None}})
    assert c3(sender("Asha Menon"), cl).unchecked_reason == "no_data"
    cl = FakeClient({"/person/identify": NotEnabled("x"),
                     "/person/search": {"profiles": [{"crustdata_person_id": 1, "basic_profile": {"name": "Asha Menon"},
                                                      "experience": None}]}})
    assert c3(sender("Asha Menon"), cl).status == "unchecked"


def test_unicode_names_fold():
    p = person("finance_current")
    p["basic_profile"]["name"] = "Ásha Ménon"
    assert c3(sender("Asha Menon"), client_with(p)).status == "verified"


# ---------------------------------------------------------------- runner + insider (P1)

def test_check_signatory_runner_without_insider():
    rows = check_signatory(sender("Asha Menon"), ctx(), client_with(person("finance_current")), None)
    assert [r.check for r in rows] == ["C3_signatory"]       # insider off: buyer_domain empty


def _with_buyer():
    pol = {k: dict(v) for k, v in policy().items()}
    pol["buyer"]["buyer_domain"] = "ourco.example"
    return pol


def test_insider_vendor_led_by_our_employee(graph):
    enrich = [{"matches": [{"confidence_score": 1, "company_data": {"people": {
        "founders": [{"crustdata_person_id": 9007, "basic_profile": {"name": "Karan Mehta"}}], "cxos": []}}}]}]
    insider = person("elsewhere")
    cl = FakeClient({"/person/identify": NotEnabled("403"), "/company/enrich": enrich,
                     "/person/search": lambda b: ({"profiles": [insider], "total_count": 1}
                                                  if b["filters"]["conditions"][0]["field"] == "crustdata_person_id"
                                                  else {"profiles": [], "total_count": 0})})
    rows = {r.check: r for r in check_signatory(sender("Asha Menon"), ctx(policy=_with_buyer()), cl, graph)}
    assert rows["C3b_insider"].status == "contradicted" and "ourco.example" in rows["C3b_insider"].detail
    assert any(e["rel"] == "LEADS" for e in graph.edges_for_case("c3case"))


def test_insider_skipped_for_known_vendor(seeded):
    cl = client_with()
    rows = {r.check: r for r in check_signatory(sender("Asha Menon"), ctx(policy=_with_buyer()), cl, seeded)}
    assert rows["C3b_insider"].unchecked_reason == "not_applicable" and cl.count("/company/enrich") == 0


def test_signatory_crash_isolated(monkeypatch):
    import payeeguard.checks_signatory as cs
    monkeypatch.setattr(cs, "_vendor_facts", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    rows = check_signatory(sender("Asha Menon"), ctx(), client_with(), None)
    assert rows[0].status == "unchecked" and rows[0].unchecked_reason == "api_error"
