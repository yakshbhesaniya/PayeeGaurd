"""Spec 13.6: payment gate and human-in-the-loop (P1-P5), G-3, G-7."""
import json
import os

import pytest
from fakes import FAKE_CHECKS, sample

from payeeguard import gate
from payeeguard.pipeline import analyse, finish


def verify(name_or_text, graph, monkeypatch=None, **env):
    text = sample(name_or_text) if name_or_text.endswith(".txt") else name_or_text
    c = analyse(text, client=None, graph=graph, checks=FAKE_CHECKS)
    finish(c)
    return c.verdict


def outbox():
    path = os.path.join(os.environ["PAYEEGUARD_PAYMENTS_DIR"], "outbox.jsonl")
    return open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []


def test_gate_unknown_case():
    assert gate.pay_invoice("deadbeef0000").startswith("Error: unknown case_id") and not outbox()
    assert gate.pay_invoice("../../etc/passwd").startswith("Error") and not outbox()


@pytest.mark.parametrize("name,status", [("04_new_account_real_domain.txt", "HOLD"), ("02_lookalike.txt", "BLOCK")])
def test_gate_refuses(graph, name, status):
    v = verify(name, graph)
    msg = gate.pay_invoice(v.case_id, graph=graph)
    assert msg.startswith("Refused") and status in msg and v.decision_rule in msg and not outbox()


def test_gate_pays_pay_once(graph):
    v = verify("01_legit.txt", graph)
    assert gate.pay_invoice(v.case_id, graph=graph).startswith("SIMULATED")
    line = json.loads(outbox()[0])
    assert line["simulated"] is True and "000123456789" not in outbox()[0]
    assert "already PAID_SIMULATED" in gate.pay_invoice(v.case_id, graph=graph)
    assert len(outbox()) == 1


def test_gate_no_double_pay_after_reverify(graph):
    """G-3: same text verified again keeps PAID_SIMULATED."""
    v = verify("01_legit.txt", graph)
    gate.pay_invoice(v.case_id, graph=graph)
    verify("01_legit.txt", graph)
    assert gate.load_case(v.case_id)["status"] == "PAID_SIMULATED"
    assert "already PAID_SIMULATED" in gate.pay_invoice(v.case_id, graph=graph)


def test_resolve_pay_case(graph):
    """P3."""
    v = verify("01_legit.txt", graph)
    assert "already payable" in gate.resolve_case(v.case_id, "approve", "asha")


def test_hold_approve_then_pay_teaches_graph(graph):
    """G-8: approved bank change -> new account gets PAID edges; old account stays."""
    v = verify("04_new_account_real_domain.txt", graph)
    assert "approved" in gate.resolve_case(v.case_id, "approve", "asha", "called +1 855, confirmed", graph=graph)
    assert gate.pay_invoice(v.case_id, graph=graph).startswith("SIMULATED")
    paid = graph.accounts_paid("company:6042422")
    assert len(paid) == 2


def test_approval_not_transferable(graph):
    """P4: a new email with different details is a new case; the approval does not carry over."""
    v = verify("04_new_account_real_domain.txt", graph)
    gate.resolve_case(v.case_id, "approve", "asha", graph=graph)
    v2 = verify(sample("04_new_account_real_domain.txt").replace("GB33 BUKB 2020 1555 5555 55",
                                                                 "GB82 WEST 1234 5698 7654 32"), graph)
    assert v2.case_id != v.case_id
    assert gate.pay_invoice(v2.case_id, graph=graph).startswith("Refused")


def test_dual_approval_distinct(graph):
    """P5: above dual_approval_above_usd two different reviewers are needed, even on PAY."""
    text = sample("01_legit.txt").replace("USD 4,200.00", "USD 30,000.00")
    v = verify(text, graph)
    assert v.decision == "PAY"            # fakes have no C9 amount check; dual approval still applies
    assert gate.pay_invoice(v.case_id, graph=graph).startswith("Refused")
    assert "1 of 2" in gate.resolve_case(v.case_id, "approve", "asha", graph=graph)
    assert "different reviewer" in gate.resolve_case(v.case_id, "approve", "asha", graph=graph)
    assert gate.pay_invoice(v.case_id, graph=graph).startswith("Refused")
    assert "approved" in gate.resolve_case(v.case_id, "approve", "ravi", graph=graph)
    assert gate.pay_invoice(v.case_id, graph=graph).startswith("SIMULATED")


def test_hold_reject_becomes_block_with_case_file(graph):
    v = verify("04_new_account_real_domain.txt", graph)
    assert "now BLOCK" in gate.resolve_case(v.case_id, "reject", "asha", "vendor denied", graph=graph)
    rec = gate.load_case(v.case_id)
    assert rec["status"] == "REJECTED" and rec["decision"] == "BLOCK"
    assert os.path.exists(os.path.join(os.environ["PAYEEGUARD_REPORTS_DIR"], v.case_id, "fraud_case_file.md"))
    acct = rec["account_node"]
    assert graph.cases_flagged(acct) == [v.case_id]
    assert gate.pay_invoice(v.case_id, graph=graph).startswith("Refused")


def test_override_revokes_flags(graph):
    """G-7: override on a BLOCK revokes its FLAGGED_IN edges, audited; note required."""
    v = verify("05_zoho_lookalike_mule.txt", graph)
    acct = gate.load_case(v.case_id)["account_node"]
    assert graph.cases_flagged(acct) == [v.case_id]
    assert "requires a note" in gate.resolve_case(v.case_id, "override", "asha", "", graph=graph)
    assert "Only 'override'" in gate.resolve_case(v.case_id, "approve", "asha", graph=graph)
    assert "revoked" in gate.resolve_case(v.case_id, "override", "asha", "vendor confirmed on call", graph=graph)
    assert graph.cases_flagged(acct) == []
    hero = verify("06_hero_freshworks_mule.txt", graph)
    assert hero.decision == "HOLD"            # no longer linked to a blocked case


def test_override_not_for_hold(graph):
    v = verify("04_new_account_real_domain.txt", graph)
    assert "only for blocks" in gate.resolve_case(v.case_id, "override", "asha", "x", graph=graph)


def test_resolve_validation():
    assert gate.resolve_case("x", "maybe", "asha").startswith("Error")
    assert gate.resolve_case("x", "approve", " ").startswith("Error")
    assert gate.resolve_case("abc", "approve", "asha").startswith("Error: unknown")


def test_case_status(graph):
    v = verify("02_lookalike.txt", graph)
    assert "status BLOCK" in gate.case_status(v.case_id)
    assert gate.case_status("nope").startswith("Error")


def test_case_record_has_no_raw_text_or_account(graph):
    """Y1 / Y2."""
    v = verify("06_hero_freshworks_mule.txt", graph)
    blob = json.dumps(gate.load_case(v.case_id))
    assert "regional treasury" not in blob and "12345698765432" not in blob and "GB82WEST" not in blob


def test_reject_a_pay_case(graph):
    v = verify("01_legit.txt", graph)
    assert "now BLOCK" in gate.resolve_case(v.case_id, "reject", "asha", "looks wrong", graph=graph)
    assert gate.pay_invoice(v.case_id, graph=graph).startswith("Refused")


def test_pay_survives_broken_graph(graph):
    class Broken:
        def __getattr__(self, name):
            def f(*a, **k):
                raise OSError("db locked")
            return f
    v = verify("01_legit.txt", graph)
    assert gate.pay_invoice(v.case_id, graph=Broken()).startswith("SIMULATED")


def test_approval_voided_by_new_block(graph):
    """An approved HOLD whose account is later flagged in another BLOCK can no longer be paid."""
    v = verify("04_new_account_real_domain.txt", graph)
    gate.resolve_case(v.case_id, "approve", "asha", graph=graph)
    attack = sample("05_zoho_lookalike_mule.txt").replace("GB82 WEST 1234 5698 7654 32", "GB33 BUKB 2020 1555 5555 55")
    assert verify(attack, graph).decision == "BLOCK"
    again = verify("04_new_account_real_domain.txt", graph)
    assert again.decision == "BLOCK"
    assert gate.load_case(v.case_id)["status"] == "BLOCK"
    assert gate.pay_invoice(v.case_id, graph=graph).startswith("Refused")
