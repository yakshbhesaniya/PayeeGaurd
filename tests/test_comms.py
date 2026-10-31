"""C15 relationship history with mocked Slack/mail sources."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import email, seed_freshworks
from payeeguard import comms
from payeeguard.pipeline import verify

NOW = datetime.now(timezone.utc)


@pytest.fixture
def history(monkeypatch):
    msgs = []
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(comms, "search_slack", lambda terms: [m for m in msgs if m["source"] == "slack"])
    monkeypatch.setattr(comms, "search_mail", lambda terms: [m for m in msgs if m["source"] == "email"])
    comms._CACHE.clear()
    return msgs


def _m(source, days_ago, text, where="#finance", frm=""):
    return {"source": source, "where": where, "when": NOW - timedelta(days=days_ago), "text": text,
            "cite": f"{source}:{where}@{days_ago}", "from": frm}


def checks(v):
    return {e.check: e for e in v.evidence}


def test_terminated_vendor_flagged(client, graph, history):
    seed_freshworks(graph)
    history.append(_m("slack", 90, "We will not continue with Freshworks after this quarter, tell them not to send invoices"))
    v, _ = verify(email(body="Pay to account on file."), client=client, graph=graph, save=False)
    e = checks(v)["C15_relationship"]
    assert e.status == "contradicted" and e.weight == 45 and "#finance" in e.detail
    assert v.decision != "PAY" and v.hypothesis.attack_type == "terminated_vendor"


def test_renewal_after_termination_is_fine(client, graph, history):
    seed_freshworks(graph)
    history.append(_m("slack", 90, "We will not continue with Freshworks"))
    history.append(_m("email", 10, "Freshworks contract renewed for 2027", where="INBOX: Renewal"))
    v, _ = verify(email(body="Pay to account on file."), client=client, graph=graph, save=False)
    assert checks(v)["C15_relationship"].status == "verified" and v.decision == "PAY"


def test_no_history_mentions_is_verified(client, graph, history):
    history.append(_m("slack", 30, "Lunch at 1?"))
    v, _ = verify(email(), client=client, graph=graph, save=False)
    assert checks(v)["C15_relationship"].status == "verified"


def test_source_failure_is_unchecked_not_fraud(client, graph, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    comms._CACHE.clear()
    monkeypatch.setattr(comms, "search_slack", lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(comms, "search_mail", lambda t: [])
    v, _ = verify(email(), client=client, graph=graph, save=False)
    e = checks(v)["C15_relationship"]
    assert e.status == "unchecked" and e.weight == 0


def test_not_connected_is_not_applicable(client, graph):
    v, _ = verify(email(), client=client, graph=graph, save=False)
    assert checks(v)["C15_relationship"].unchecked_reason == "not_applicable"


def test_vendor_term_matching():
    assert comms._mentions("we're done with Freshworks.", ["freshworks"])
    assert not comms._mentions("freshworksish", ["freshworks"])
