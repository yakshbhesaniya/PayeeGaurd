"""Graph and memory (spec 8, 13.4): fingerprints, idempotent writes, own-case exclusion, revocation, seed."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import FRESHWORKS, ZOHO, FakeClient, OfflineMiss, identify_client, sample
from payeeguard import graph as graph_mod
from payeeguard.checks_payment import check_payment
from payeeguard.graph import (Graph, GraphConfigError, SeedError, case_id_for, load_seed, normalise_bank_code,
                              normalise_domain, remit_country, vendor_node)

FW, ZH = f"company:{FRESHWORKS}", f"company:{ZOHO}"
MULE = "GB82 WEST 1234 5698 7654 32"


# ---------------------------------------------------------------- G-11, G-10

@pytest.mark.parametrize("secret", ["", "   ", "short"])
def test_fp_secret_required(monkeypatch, tmp_path, secret):
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", secret)
    with pytest.raises(GraphConfigError, match="PAYEEGUARD_FP_SECRET"):
        Graph(tmp_path / "g.db")


def test_fp_secret_missing_env(monkeypatch, tmp_path):
    monkeypatch.delenv("PAYEEGUARD_FP_SECRET", raising=False)
    with pytest.raises(GraphConfigError, match="missing"):
        Graph(tmp_path / "g.db")


def test_graph_autocreate(tmp_path):
    path = tmp_path / "deep" / "nested" / "graph.db"
    g = Graph(path)
    assert path.exists()
    assert g._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    g.add_node("case:x", "case", "x")
    g.close()
    g2 = Graph(path)                              # reopening an existing file keeps data
    assert g2.node_label("case:x") == "x"
    g2.close()


def test_get_graph_one_connection_per_file(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_mod, "_GRAPHS", {})
    a = graph_mod.get_graph(tmp_path / "g.db")
    b = graph_mod.get_graph(str(tmp_path / "g.db"))
    assert a is b
    a.close()


def test_locked_database_waits_instead_of_failing(tmp_path):
    g1, g2 = Graph(tmp_path / "g.db"), Graph(tmp_path / "g.db")
    g1._db.execute("BEGIN IMMEDIATE")
    g1._db.execute("INSERT INTO nodes VALUES('n:1','t','l','now')")
    import threading
    threading.Timer(0.3, g1._db.commit).start()
    g2.add_node("n:2", "t", "l")                  # blocks until g1 commits, then succeeds
    assert g2.node_label("n:2") == "l"
    g1.close(), g2.close()


# ---------------------------------------------------------------- fingerprints (E12, G-5)

def test_bank_normalisation(graph):
    a = graph.fingerprint(iban="GB82 WEST 1234 5698 7654 32")
    b = graph.fingerprint(iban="gb82west12345698765432")
    c = graph.fingerprint(iban="IBAN: GB82-WEST-1234-5698-7654-32")
    assert a.strong == b.strong == c.strong and a.label == "••5432 GB"
    d = graph.fingerprint(account="0001-2345-6789", code="chasus33")
    e = graph.fingerprint(account="000123456789", code="CHASUS33XXX")
    assert set(d.strong) == set(e.strong)
    assert normalise_bank_code("HDFC0000001") == "HDFC0000001"      # IFSC kept as is


def test_fingerprint_is_keyed(tmp_path):
    g1 = Graph(":memory:", secret="secret-one-0123456789")
    g2 = Graph(":memory:", secret="secret-two-0123456789")
    assert g1.fingerprint(iban=MULE).strong != g2.fingerprint(iban=MULE).strong


def test_fingerprint_kinds(graph):
    weak_only = graph.fingerprint(code="CHASUS33", last4="6789")
    assert not weak_only.strong and weak_only.weak and weak_only.weak.startswith("account:weak:")
    no_code = graph.fingerprint(account="000123456789")
    assert not no_code.strong and no_code.alias and no_code.weak is None    # code-less alias only
    with_code = graph.fingerprint(account="000123456789", code="CHASUS33")
    assert with_code.alias == no_code.alias and with_code.strong
    assert graph.fingerprint(account="12345", code="CHASUS33").alias is None   # too short to alias
    assert graph.fingerprint().empty
    assert graph.fingerprint(last4="6789").empty                   # last 4 without a bank code is nothing


def test_remit_country_order():
    assert remit_country("gb", "DE89370400440532013000", "CHASUS33") == "GB"
    assert remit_country(None, "DE89370400440532013000", "CHASUS33") == "DE"
    assert remit_country(None, None, "CHASUS33") == "US"
    assert remit_country(None, None, "HDFC0000001") == "IN"
    assert remit_country(None, None, None) is None


def test_node_helpers():
    assert normalise_domain("https://www.Freshworks.com/contact/") == "freshworks.com"
    assert normalise_domain("billing@freshworks.com.") == "freshworks.com"
    assert vendor_node(FRESHWORKS) == FW and vendor_node("6042422") == FW
    assert vendor_node(None, "WWW.Acme.co.in") == "vendor:acme.co.in"
    assert vendor_node(None, None) is None
    assert case_id_for("a  b\n c") == case_id_for("a b c")


def test_no_full_account_in_outputs(seeded, tmp_path):
    """Y2: the database never holds a raw account number or IBAN."""
    i, ctx = sample("06_hero_freshworks_mule.txt")
    check_payment(i, ctx, None, seeded)
    seeded.record_block(ctx["case_id"], seeded.fingerprint_invoice(i), i.sender_email, label="hero")
    seeded.close()
    blob = Path(seeded.path).read_bytes() + b"".join(
        p.read_bytes() for p in Path(seeded.path).parent.glob("graph.db*"))
    for secret in (b"000123456789", b"50200012345678", b"GB82WEST12345698765432", b"WEST 1234 5698",
                   b"billing@freshworks.com"):
        assert secret not in blob


# ---------------------------------------------------------------- writes and queries

def test_edge_requires_citation(graph):
    with pytest.raises(ValueError):
        graph.add_edge("a", "REL", "b", "", "case1")
    with pytest.raises(ValueError):
        graph.add_edge("a", "REL", "b", "cite", "")


def test_seed_loads_and_is_idempotent(graph):
    s1 = load_seed(graph, identify_client())
    assert [v["vendor"] for v in s1["vendors"]] == [FW, ZH] and s1["payments"] == 20
    s2 = load_seed(graph, identify_client())
    assert all(v["new"] == 0 for v in s2["vendors"])
    assert len(graph.payments(FW)) == 14 and len(graph.payments(ZH)) == 6
    assert graph.domains_paid_from(FW) == ["freshworks.com"]
    edges = graph._rows("SELECT citation FROM edges WHERE rel IN ('PAID','PAID_FROM_DOMAIN')", ())
    assert edges and all(e["citation"] == "seed:samples/seed_history.json" for e in edges)


def test_seed_offline_message(graph):
    """G-9: offline with an empty cache -> a clear, actionable message."""
    with pytest.raises(SeedError, match=r"freshworks\.com.*OfflineMiss.*\.cache"):
        load_seed(graph, FakeClient({"/company/identify": OfflineMiss("cache miss")}))
    with pytest.raises(SeedError, match="client"):
        load_seed(graph, None)


def test_seed_unknown_vendor_falls_back_to_domain(graph, tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text('{"vendors":[{"name":"Tiny Co","domain":"tiny.example","currency":"INR",'
                    '"account":{"ifsc":"ICIC0001234","account":"123456789"},'
                    '"payments":[{"invoice_number":"T-1","amount":100,"paid_on":"2026-01-01"}]}]}', encoding="utf-8")
    load_seed(graph, identify_client(), seed)
    assert graph.vendor_known("vendor:tiny.example")


def test_seed_bad_file(graph, tmp_path):
    with pytest.raises(SeedError, match="cannot read"):
        load_seed(graph, identify_client(), tmp_path / "missing.json")


def test_query_excludes_own_case(seeded):
    """G-1: re-running 05 must not see its own FLAGGED_IN."""
    fp = seeded.fingerprint(iban=MULE)
    seeded.record_block("case05", fp, label="Zoho impersonation BLOCKED")
    assert seeded.cases_flagged(fp.strong, exclude_case="case05") == []
    assert len(seeded.cases_flagged(fp.matchable, exclude_case="case06")) == len(fp.matchable)
    seeded.record_payment("caseP", FW, fp, "X-1", 10, "USD", "freshworks.com", "2026-10-01")
    assert fp.strong[0] not in seeded.accounts_paid(FW, exclude_case="caseP")
    assert seeded.invoice_paid(FW, "X-1", exclude_case="caseP") == []
    assert seeded.invoice_paid(FW, "x-1", exclude_case="other")          # invoice no. normalised


def test_verify_idempotent(seeded):
    """G-2: verify writes only SENT / CLAIMS_VENDOR / PAYS_TO, idempotently; same verdict twice."""
    i, ctx = sample("04_new_account_real_domain.txt")
    first = check_payment(i, dict(ctx), None, seeded)
    n_edges = seeded._rows("SELECT COUNT(*) c FROM edges", ())[0]["c"]
    second = check_payment(i, dict(ctx), None, seeded)
    assert first == second
    assert seeded._rows("SELECT COUNT(*) c FROM edges", ())[0]["c"] == n_edges
    rels = {e["rel"] for e in seeded.edges_for_case(ctx["case_id"])}
    assert rels == {"SENT", "CLAIMS_VENDOR", "PAYS_TO"}
    assert not seeded.is_case_paid(ctx["case_id"])


def test_gate_no_double_pay(seeded):
    """G-3: paying the same case twice records one payment; the gate can ask is_case_paid."""
    i, ctx = sample("01_legit.txt")
    fp = seeded.fingerprint_invoice(i)
    assert seeded.record_payment(ctx["case_id"], FW, fp, i.invoice_number, i.amount, i.currency, "freshworks.com")
    assert not seeded.record_payment(ctx["case_id"], FW, fp, i.invoice_number, i.amount, i.currency, "freshworks.com")
    assert seeded.is_case_paid(ctx["case_id"])
    assert len(seeded.invoice_paid(FW, "INV-2026-1043", exclude_case="another")) == 1


def test_record_payment_needs_vendor_and_account(graph):
    with pytest.raises(ValueError):
        graph.record_payment("c", "", graph.fingerprint(iban=MULE), "1", 1, "USD")
    with pytest.raises(ValueError):
        graph.record_payment("c", FW, graph.fingerprint(), "1", 1, "USD")


def test_override_revokes_flags(seeded):
    """G-7: an overridden BLOCK stops flagging its account; re-blocking the same case stays revoked."""
    fp = seeded.fingerprint(iban=MULE)
    seeded.record_block("case05", fp, "a@zoh0-billing.com", "zoh0-billing.com", "Zoho impersonation BLOCKED")
    assert seeded.cases_flagged(fp.strong, "case06")
    assert seeded.revoke_case("case05") == len(fp.matchable) + 2
    assert seeded.cases_flagged(fp.strong, "case06") == []
    seeded.record_block("case05", fp)              # idempotent write does not un-revoke
    assert seeded.cases_flagged(fp.strong, "case06") == []
    assert seeded.revoke_case("case05") == 0


def test_block_does_not_flag_real_domain_by_default(seeded):
    seeded.record_block("case06", seeded.fingerprint(iban=MULE), "billing@freshworks.com")
    assert seeded.cases_flagged(["domain:freshworks.com"]) == []


def test_approved_bank_change(seeded):
    """G-8: a human-approved new account joins the history; the old one still verifies."""
    new = seeded.fingerprint(iban="GB33 BUKB 2020 1555 5555 55")
    old = seeded.fingerprint(account="000123456789", code="CHASUS33")
    seeded.record_payment("case04", FW, new, "INV-2026-1101", 4200, "USD", "freshworks.com", "2026-10-31")
    paid = set(seeded.accounts_paid(FW, "later"))
    assert set(new.strong) <= paid and set(old.strong) <= paid


def test_money_trail_hero(seeded):
    i5, c5 = sample("05_zoho_lookalike_mule.txt")
    check_payment(i5, c5, None, seeded)
    seeded.record_block(c5["case_id"], seeded.fingerprint_invoice(i5), label="Zoho impersonation BLOCKED")
    i6, c6 = sample("06_hero_freshworks_mule.txt")
    check_payment(i6, c6, None, seeded)
    rows = c6["graph_edges"]
    rels = [r["rel"] for r in rows]
    assert {"CLAIMS_VENDOR", "PAYS_TO", "PAID", "FLAGGED_IN"} <= set(rels)
    paid = next(r for r in rows if r["rel"] == "PAID")
    assert paid["count"] == 14 and "6789" in paid["dst_label"]
    flag = next(r for r in rows if r["rel"] == "FLAGGED_IN")
    assert flag["dst_label"] == "Zoho impersonation BLOCKED" and flag["citation"]
    assert all(r["citation"] for r in rows)


def test_reset(seeded):
    seeded.reset()
    assert seeded.payments(FW) == [] and not seeded.vendor_known(FW)


def test_main_seed_reports_missing_secret(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", "")
    monkeypatch.setattr(graph_mod, "_GRAPHS", {})
    monkeypatch.setattr(graph_mod, "REPO_ROOT", tmp_path)          # no .env refills the secret
    assert graph_mod.main(["seed", "--db", str(tmp_path / "g.db")]) == 1
    assert "PAYEEGUARD_FP_SECRET" in capsys.readouterr().err


def test_sqlite_file_is_plain_sqlite(seeded):
    seeded.close()
    con = sqlite3.connect(seeded.path)
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"nodes", "edges",
                                                                                                 "payments"}
    con.close()
