"""Spec 9: pattern library, ranking (S7), ruled-out list, cited narratives."""
from test_scoring import C1, C2, E, G1, G1X, inv

from payeeguard.investigator import investigate

LOOKALIKE = E("C2_domain", "CHANNEL", hard=True, detail="freshw0rks-billing.com is a lookalike of freshworks.com")
G3 = E("G3_account_flagged", "PAYMENT", hard=True, detail="account appears in blocked case a71f")


def attack(ev, invoice=None):
    h, also, ruled = investigate(ev, invoice or inv())
    return h.attack_type, [a.attack_type for a in also], {r.attack_type for r in ruled}


def test_hero_mule_plus_compromised():
    primary, also, ruled = attack([C1, C2, E("C5_geo", "PAYMENT", "verified", detail="GB office present"), G3, G1X])
    assert primary == "mule_campaign" and "compromised_vendor_mailbox" in also
    assert {"vendor_impersonation", "unverifiable_vendor", "wrong_geography"} <= ruled


def test_mule_beats_compromised_even_with_more_support():
    """Block-grade patterns rank first regardless of supporting count."""
    ev = [C1, C2, G3, E("C14_urgency", "BEHAVIOUR", weight=5), E("C5_geo", "PAYMENT", weight=20)]
    assert attack(ev, inv(bank_change=True))[0] == "mule_campaign"


def test_impersonation():
    assert attack([C1, LOOKALIKE])[0] == "vendor_impersonation"


def test_reply_to_lookalike_is_impersonation():
    assert attack([C1, C2, E("C10_reply_to", "CHANNEL", hard=True, detail="reply-to lookalike")])[0] \
        == "vendor_impersonation"


def test_compromised_mailbox():
    primary, _, ruled = attack([C1, C2, G1X])
    assert primary == "compromised_vendor_mailbox" and "vendor_impersonation" in ruled


def test_duplicate_replay():
    assert attack([C1, C2, G1, E("G4_duplicate", "PAYMENT", weight=40, detail="INV-2026-1031 already PAID")])[0] \
        == "duplicate_replay"


def test_ex_employee():
    assert attack([C1, E("C3_signatory", "SIGNATORY", weight=35, detail="ex-employee: left Freshworks 2024-12"), G1X])[0] \
        == "ex_employee"


def test_unauthorized_change():
    assert attack([C1, C2, E("C3_signatory", "SIGNATORY", weight=20, detail="role authority: engineer")])[0] \
        == "unauthorized_change"


def test_unverified_signatory():
    ev = [C1, E("C2b_freemail", "CHANNEL", weight=25, detail="free mail gmail.com"),
          E("C3_signatory", "SIGNATORY", "unchecked", reason="no_data")]
    assert attack(ev)[0] == "unverified_signatory"
    assert attack(ev + [E("C3_signatory", "SIGNATORY", "verified")])[0] != "unverified_signatory"


def test_unverifiable_vendor_only_on_no_data():
    assert attack([E("C1_vendor", "COMPANY", "unchecked", reason="no_data")])[0] == "unverifiable_vendor"
    assert attack([E("C1_vendor", "COMPANY", "unchecked", reason="api_error")])[0] == "none"


def test_payment_method():
    assert attack([C1, C2, G1, E("C14_payment_method", "BEHAVIOUR", weight=25, detail="payment_method: USDT")])[0] \
        == "payment_method_fraud"


def test_multiple_patterns():
    """S7: primary by priority, the rest in also_matched."""
    primary, also, _ = attack([LOOKALIKE, G3, G1X])
    assert primary == "mule_campaign" and also == ["vendor_impersonation"]


def test_none_when_clean():
    h, also, _ = investigate([C1, C2, G1], inv())
    assert h.attack_type == "none" and not also


def test_narrative_uses_cited_details():
    h, _, _ = investigate([C1, C2, G3, G1X], inv())
    assert "account appears in blocked case a71f" in h.narrative
    assert set(h.supporting) >= {"G3_account_flagged", "G1_account_history"}


def test_c3_no_data_never_ex_employee():
    """Absence is never fraud: an unchecked C3 can't produce a signatory attack."""
    assert attack([C1, C2, G1, E("C3_signatory", "SIGNATORY", "unchecked", weight=35, reason="no_data",
                                 detail="ex-employee?")])[0] == "none"
