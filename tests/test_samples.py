"""All 10 samples end to end through Bahul's pipeline with fake checks (spec 17, expected.json)."""
import json
import os

import pytest
from fakes import FAKE_CHECKS, ROOT, sample

from payeeguard import gate
from payeeguard.pipeline import analyse, finish

EXPECTED = {k: v for k, v in json.loads((ROOT / "samples" / "expected.json").read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


def run_all_samples(graph, monkeypatch):
    out = {}
    for name in sorted(EXPECTED):
        with monkeypatch.context() as m:
            for k, v in EXPECTED[name].get("env", {}).items():
                m.setenv(k, v)
            case = analyse(sample(name), client=None, graph=graph, checks=FAKE_CHECKS)
            finish(case)
        out[name] = case
    return out


def test_all_samples_hit_expected(graph, monkeypatch):
    cases = run_all_samples(graph, monkeypatch)
    got = {n: (c.verdict.decision, c.verdict.hypothesis.attack_type) for n, c in cases.items()}
    want = {n: (e["decision"], e["attack_type"]) for n, e in EXPECTED.items()}
    assert got == want


def test_hero_sequence_links_campaign(graph, monkeypatch):
    cases = run_all_samples(graph, monkeypatch)
    hero, zoho = cases["06_hero_freshworks_mule.txt"].verdict, cases["05_zoho_lookalike_mule.txt"].verdict
    assert hero.decision_rule.startswith("R1 hard fact G3")
    assert "compromised_vendor_mailbox" in [h.attack_type for h in hero.also_matched]
    assert zoho.case_id in cases["06_hero_freshworks_mule.txt"].ctx["related_cases"]
    ruled = {r.attack_type for r in hero.ruled_out}
    assert {"vendor_impersonation", "unverifiable_vendor", "wrong_geography"} <= ruled   # S6
    path = os.path.join(os.environ["PAYEEGUARD_REPORTS_DIR"], hero.case_id, "fraud_case_file.md")
    text = open(path, encoding="utf-8").read()
    assert zoho.case_id in text and "Campaign: 2 case(s)" in text
    assert "GB82" not in text and "12345698765432" not in text                           # Y2


def test_hero_without_05_is_hold_not_block(graph):
    v = analyse(sample("06_hero_freshworks_mule.txt"), client=None, graph=graph, checks=FAKE_CHECKS).verdict
    assert v.decision == "HOLD" and v.hypothesis.attack_type == "compromised_vendor_mailbox"


def test_rerun_05_does_not_match_itself(graph):
    """G-1 / G-2: a BLOCK writes FLAGGED_IN, but re-running the same text gives the same verdict."""
    a = analyse(sample("05_zoho_lookalike_mule.txt"), client=None, graph=graph, checks=FAKE_CHECKS).verdict
    b = analyse(sample("05_zoho_lookalike_mule.txt"), client=None, graph=graph, checks=FAKE_CHECKS).verdict
    assert (a.decision, a.risk_score, a.decision_rule) == (b.decision, b.risk_score, b.decision_rule)
    assert not any(e.check.startswith("G3") for e in b.evidence)


def test_deterministic(graph):
    """S11: identical verdict, rule and report text except the timestamp line."""
    from payeeguard.report import render
    texts = []
    for _ in range(2):
        c = analyse(sample("04_new_account_real_domain.txt"), client=None, graph=graph, checks=FAKE_CHECKS)
        texts.append("\n".join(line for line in render(c.verdict, c.invoice, c.ctx).split("\n") if "IST" not in line))
    assert texts[0] == texts[1]


def test_api_down_holds(graph, monkeypatch):
    """S1 / sample 10: Crustdata down -> LOW -> R7 HOLD with 'could not verify', never BLOCK or PAY."""
    monkeypatch.setenv("FAKE_API_DOWN", "1")
    v = analyse(sample("01_legit.txt"), client=None, graph=graph, checks=FAKE_CHECKS).verdict
    assert v.decision == "HOLD" and v.confidence == "LOW" and v.decision_rule.startswith("R7")
    assert "could not verify" in v.summary


def test_unknown_vendor_holds(graph):
    """S3 / sample 07: unknown vendor -> HOLD + KYB task, not BLOCK."""
    c = analyse(sample("07_unknown_vendor.txt"), client=None, graph=graph, checks=FAKE_CHECKS)
    finish(c)
    assert c.verdict.decision == "HOLD"
    assert "kyb_onboarding_task" in [a.kind for a in c.verdict.actions]


def test_legit_pays_and_gate_releases(graph):
    c = analyse(sample("01_legit.txt"), client=None, graph=graph, checks=FAKE_CHECKS)
    finish(c)
    assert c.verdict.decision == "PAY" and c.verdict.confidence == "HIGH"
    msg = gate.pay_invoice(c.verdict.case_id, graph=graph)
    assert msg.startswith("SIMULATED payment")
    assert "already PAID_SIMULATED" in gate.pay_invoice(c.verdict.case_id, graph=graph)   # G-3


@pytest.mark.parametrize("name", ["02_lookalike.txt", "08_prompt_injection.txt"])
def test_lookalikes_block(graph, name):
    v = analyse(sample(name), client=None, graph=graph, checks=FAKE_CHECKS).verdict
    assert v.decision == "BLOCK" and v.decision_rule.startswith("R1")
