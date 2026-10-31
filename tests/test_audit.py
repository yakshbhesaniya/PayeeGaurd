"""Tamper-evident audit log (spec 11, 13.6 P6, 13.8 Y1-Y3)."""
from __future__ import annotations

import json
import threading

from payeeguard import audit


def _events(path, n=3):
    return [audit.append({"type": "verdict", "case_id": f"c{i}", "decision": "HOLD"}, path) for i in range(n)]


def test_append_and_verify(tmp_path):
    p = tmp_path / "reports" / "audit.jsonl"
    hashes = _events(p)
    ok, n, msg = audit.verify(p)
    assert ok and n == 3 and "intact" in msg
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["prev_hash"] == audit.GENESIS and lines[1]["prev_hash"] == hashes[0]
    assert audit.last_hash(p) == hashes[-1]


def test_audit_tamper_detected(tmp_path):
    """P6: editing, deleting or reordering lines is reported at the first broken line."""
    p = tmp_path / "audit.jsonl"
    _events(p, 4)
    original = p.read_text(encoding="utf-8").splitlines()

    assert '"decision":"HOLD"' in original[0]
    p.write_text("\n".join(original).replace('"decision":"HOLD"', '"decision":"PAY"', 1) + "\n", encoding="utf-8")
    ok, n, msg = audit.verify(p)
    assert not ok and n == 0 and msg.startswith("line 1") and "edited" in msg

    p.write_text("\n".join(original[:1] + original[2:]) + "\n", encoding="utf-8")
    ok, n, msg = audit.verify(p)
    assert not ok and n == 1 and msg.startswith("line 2") and "chain broken" in msg

    p.write_text("\n".join([original[1], original[0]] + original[2:]) + "\n", encoding="utf-8")
    assert not audit.verify(p)[0]

    p.write_text("\n".join(original[:2] + ["{not json"] + original[2:]) + "\n", encoding="utf-8")
    ok, _, msg = audit.verify(p)
    assert not ok and msg.startswith("line 3")


def test_truncating_the_tail_is_undetectable_but_chain_stays_valid(tmp_path):
    """Documented limit: dropping the newest lines leaves a valid prefix; the case file quotes last_hash."""
    p = tmp_path / "audit.jsonl"
    hashes = _events(p, 3)
    p.write_text("\n".join(p.read_text(encoding="utf-8").splitlines()[:2]) + "\n", encoding="utf-8")
    assert audit.verify(p)[0] and audit.last_hash(p) != hashes[-1]


def test_missing_log_is_ok(tmp_path):
    ok, n, _ = audit.verify(tmp_path / "none.jsonl")
    assert ok and n == 0


def test_sanitise_never_writes_secrets(tmp_path):
    """Y1/Y2/Y3: raw text dropped, full numbers masked, labels kept."""
    p = tmp_path / "a.jsonl"
    audit.append({"type": "verdict", "raw_text": "Dear customer IBAN GB82...", "authorization": "Bearer sk-123",
                  "bank_iban": "GB82 WEST 1234 5698 7654 32", "nested": {"bank_account": "000123456789"},
                  "account": "••5432 GB", "items": [{"iban": "DE89370400440532013000"}]}, p)
    text = p.read_text(encoding="utf-8")
    for leaked in ("Dear customer", "sk-123", "GB82 WEST", "000123456789", "DE8937040044"):
        assert leaked not in text
    rec = json.loads(text)
    assert rec["bank_iban"] == "••5432" and rec["nested"]["bank_account"] == "••6789"
    assert rec["account"] == "••5432 GB" and rec["items"][0]["iban"] == "••3000"
    assert audit.verify(p)[0]


def test_concurrent_appends_keep_one_chain(tmp_path):
    """M6: two writers at once never fork the chain."""
    p = tmp_path / "audit.jsonl"
    threads = [threading.Thread(target=_events, args=(p, 10)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok, n, msg = audit.verify(p)
    assert ok and n == 40, msg


def test_damaged_line_does_not_stop_logging(tmp_path):
    p = tmp_path / "audit.jsonl"
    _events(p, 1)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    audit.append({"type": "x"}, p)                  # still appends
    assert not audit.verify(p)[0]                   # and verify reports the damage


def test_unicode_and_cli(tmp_path, capsys):
    p = tmp_path / "audit.jsonl"
    audit.append({"type": "note", "reviewer": "Opéra ₹"}, p)
    assert "Opéra ₹" in p.read_text(encoding="utf-8")
    assert audit.main(["verify", str(p)]) == 0
    assert "intact" in capsys.readouterr().out
    assert audit.main([]) == 2
