"""Spec 10.5 expected outcomes and 13.5 decision rows, on hand-built Evidence."""
import pytest

from payeeguard.models import Evidence, Invoice
from payeeguard.scoring import decide, decide_onboarding, family_risk, risk_score


def E(check, family, status="contradicted", weight=0, hard=False, confirms=None, reason=None, detail="d"):
    return Evidence(check, family, "claim", status, detail, "src", weight=weight, hard_block=hard,
                    unchecked_reason=reason, confirms=confirms)


def inv(amount=4200.0, currency="USD", bank_change=False):
    return Invoice("Freshworks Inc", "freshworks.com", "billing@freshworks.com", None, None, "US", None, amount,
                   currency, bank_change_requested=bank_change)


C1 = E("C1_vendor", "COMPANY", "verified", confirms="who")
C2 = E("C2_domain", "CHANNEL", "verified", confirms="channel")
G1 = E("G1_account_history", "PAYMENT", "verified", confirms="money")
G1X = E("G1_account_history", "PAYMENT", weight=25)
C5 = E("C5_geo", "PAYMENT", "verified")
C1_NODATA = E("C1_vendor", "COMPANY", "unchecked", reason="no_data")


ROWS = [
    # name, invoice, evidence, ctx, risk, confidence, rule prefix, decision
    ("01 legit", inv(), [C1, C2, G1], {}, 0, "HIGH", "R5", "PAY"),
    ("02 lookalike", inv(18400), [C1, E("C2_domain", "CHANNEL", hard=True)], {}, None, None, "R1", "BLOCK"),
    ("03 gmail", inv(345000, "INR", True), [C1, E("C2b_freemail", "CHANNEL", weight=25),
                                             E("C3_signatory", "SIGNATORY", "unchecked", reason="no_data"),
                                             E("C14_channel_switch", "BEHAVIOUR", weight=5)], {}, 30, None, "R3", "HOLD"),
    ("04 new account", inv(), [C1, C2, G1X, C5], {}, 25, None, "R3", "HOLD"),
    ("04 + no GB office", inv(), [C1, C2, G1X, E("C5_geo", "PAYMENT", weight=20)], {}, 25, None, "R3", "HOLD"),
    ("05 zoho lookalike", inv(210000, "INR", True), [E("C2_domain", "CHANNEL", hard=True)], {}, None, None, "R1", "BLOCK"),
    ("06 hero", inv(4350), [C1, C2, E("C5_geo", "PAYMENT", "verified", confirms="money"),
                            E("G3_account_flagged", "PAYMENT", hard=True), G1X], {}, None, None, "R1", "BLOCK"),
    ("07 unknown vendor", inv(185000, "INR", True), [C1_NODATA], {}, 0, "LOW", "R3", "HOLD"),
    ("08 injection", inv(9800, "EUR"), [E("C2_domain", "CHANNEL", hard=True), E("X1_extraction", "INTEGRITY", weight=10)],
     {}, None, None, "R1", "BLOCK"),
    ("09 duplicate", inv(), [C1, C2, G1, E("G4_duplicate", "PAYMENT", weight=40)], {}, 40, "HIGH", "R4", "HOLD"),
    ("10 api down", inv(), [E(c, f, "unchecked", reason="api_error") for c, f in
                            [("C1_vendor", "COMPANY"), ("C2_domain", "CHANNEL"), ("C3_signatory", "SIGNATORY")]],
     {}, 0, "LOW", "R7", "HOLD"),
    ("small known vendor", inv(1200), [C1_NODATA, C2, G1], {"vendor_known": True}, 0, "HIGH", "R5", "PAY"),
    ("small known vendor, history channel", inv(1200), [C1_NODATA, G1], {"vendor_known": True}, 0, "MEDIUM", "R6", "PAY"),
    ("ex-employee new account", inv(), [C1, E("C3_signatory", "SIGNATORY", weight=35, detail="ex-employee"), G1X],
     {}, 60, None, "R2", "BLOCK"),
    ("engineer bank change", inv(bank_change=True), [C1, C2, E("C3_signatory", "SIGNATORY", weight=20), G1X],
     {}, 45, None, "R3", "HOLD"),
]


@pytest.mark.parametrize("name,invoice,ev,ctx,risk,conf,rule,decision", ROWS, ids=[r[0] for r in ROWS])
def test_table_10_5(name, invoice, ev, ctx, risk, conf, rule, decision):
    s = decide(invoice, ev, ctx)
    assert s.decision == decision and s.rule.startswith(rule)
    if risk is not None:
        assert s.risk == risk
    if conf is not None:
        assert s.confidence == conf


def test_no_double_count():
    """S4: free mail counts once in CHANNEL; channel_switch adds 5 in BEHAVIOUR."""
    ev = [E("C2b_freemail", "CHANNEL", weight=25), E("C2_domain", "CHANNEL", weight=15),
          E("C14_channel_switch", "BEHAVIOUR", weight=5)]
    assert family_risk(ev) == {"CHANNEL": 25, "BEHAVIOUR": 5} and risk_score(ev) == 30


def test_behaviour_capped():
    ev = [E(f"C14_t{i}", "BEHAVIOUR", weight=10) for i in range(5)]
    assert risk_score(ev) == 25


def test_risk_capped_at_100():
    ev = [E(f"X_{f}", f, weight=60) for f in ("CHANNEL", "PAYMENT", "SIGNATORY")]
    assert risk_score(ev) == 100


def test_verified_and_unchecked_never_add_risk():
    """Rule 5.1: only positive contradictions add risk, whatever weight a row carries."""
    ev = [E("C3", "SIGNATORY", "unchecked", weight=35, reason="no_data"), E("C1", "COMPANY", "verified", weight=50)]
    assert risk_score(ev) == 0


def test_context_family_zero():
    assert risk_score([E("C6_callback", "CONTEXT", weight=99)]) == 0


def test_hard_overrides():
    """S6: one hard fact blocks even with everything else verified."""
    s = decide(inv(), [C1, C2, G1, E("G3_account_flagged", "PAYMENT", hard=True)])
    assert s.decision == "BLOCK" and s.rule == "R1 hard fact G3_account_flagged"


def test_low_conf_no_attack():
    """S8: nothing contradicted but LOW -> R7 HOLD."""
    s = decide(inv(), [C1_NODATA])
    assert s.decision == "HOLD" and s.rule.startswith("R7") and s.kyb_task


def test_bank_change_no_account():
    """S5."""
    s = decide(inv(bank_change=True), [C1, C2, G1])
    assert s.decision == "HOLD" and "bank change" in s.rule


def test_ambiguous_accounts_hold():
    s = decide(inv(), [C1, C2, G1, E("G5_ambiguous_payment", "PAYMENT", weight=25)])
    assert s.decision == "HOLD" and "two different accounts" in s.rule


@pytest.mark.parametrize("weights,decision,rule", [((25,), "HOLD", "R4"), ((24,), "PAY", "R5"),
                                                   ((60,), "BLOCK", "R2"), ((59,), "HOLD", "R4")])
def test_threshold_boundaries(weights, decision, rule):
    """S9: >= for risk thresholds."""
    ev = [C1, C2, G1] + [E("C9_amount", "COMPANY", weight=w) for w in weights]
    s = decide(inv(), ev)
    assert (s.decision, s.rule[:2]) == (decision, rule)


@pytest.mark.parametrize("amount,decision", [(5000.0, "PAY"), (5000.01, "HOLD")])
def test_auto_pay_limit_boundary(amount, decision):
    """S9: <= for the auto-pay limit (MEDIUM)."""
    assert decide(inv(amount), [C1, C2]).decision == decision


def test_medium_fx_conversion():
    assert decide(inv(400000, "INR"), [C1, C2]).decision == "PAY"        # INR 400k = USD 4,800
    assert decide(inv(500000, "INR"), [C1, C2]).decision == "HOLD"       # USD 6,000


def test_unknown_currency_never_medium_pay():
    """E9."""
    s = decide(inv(10, "XYZ"), [C1, C2])
    assert s.decision == "HOLD" and "currency unknown" in s.rule


def test_geo_money_needs_valid_bank_format():
    c5 = E("C5_geo", "PAYMENT", "verified", confirms="money")
    assert decide(inv(), [C1, C2, c5]).confidence == "HIGH"
    assert decide(inv(), [C1, C2, c5, E("C11_bank_format", "PAYMENT", weight=25)]).confidence == "MEDIUM"


def test_credit_note_holds():
    assert decide(inv(), [C1, C2, G1], {"credit_note": True}).decision == "HOLD"


def test_api_error_note_only_when_low():
    s = decide(inv(), [E("C1", "COMPANY", "unchecked", reason="api_error")])
    assert "could not verify: do not pay until checks run" in s.notes


@pytest.mark.parametrize("ev,want", [
    ([E("C2_domain", "CHANNEL", hard=True)], "REJECT"),
    ([C1, E("C8_domain_age", "CHANNEL", weight=25, detail="sender domain not registered (RDAP 404)")], "REJECT"),
    ([C1_NODATA], "REVIEW"),
    ([C1, E("O_claims", "COMPANY", weight=10, detail="claimed headcount 120 vs 7")], "REVIEW"),
    ([C1, E("C8", "CHANNEL", "unchecked", reason="api_error")], "REVIEW"),
    ([C1, E("C4_shell", "COMPANY", "verified")], "APPROVE"),
    ([], "REVIEW"),
])
def test_onboarding_decision(ev, want):
    assert decide_onboarding(ev)[0] == want
