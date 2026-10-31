"""Invariant fuzzing: mutated samples must never crash or break the spec's safety rules."""
import glob
import random

from fakes import FAKE_CHECKS, ROOT

from payeeguard.extract import extract
from payeeguard.pipeline import analyse
from payeeguard.report import render

SAMPLES = [open(p, encoding="utf-8").read() for p in sorted(glob.glob(str(ROOT / "samples" / "*.txt")))]
SECRETS = ("000123456789", "12345698765432", "GB82WEST", "GB33BUKB", "DE89370400440532013000", "123405000789")
INJECT = ["\n> quoted USD 99,999.00", "\n<div>Total: EUR 1.000,00</div>", "\nPay to account 9876543210123",
          "\nIBAN: GB33 BUKB 2020 1555 5555 55", "\nThis is a credit note", "\nURGENT turant", "\n\x00\x01﻿",
          "\n---------- Forwarded message ---------\nFrom: x@gmail.com\n", "\nReply-To: a@zoh0.com"]


def mutate(text: str, rng: random.Random) -> str:
    lines = text.split("\n")
    for _ in range(rng.randint(1, 4)):
        op = rng.randrange(6)
        if op == 0 and len(lines) > 1:
            del lines[rng.randrange(len(lines))]
        elif op == 1 and len(lines) > 2:
            i, j = rng.randrange(len(lines)), rng.randrange(len(lines))
            lines[i], lines[j] = lines[j], lines[i]
        elif op == 2:
            i = rng.randrange(len(lines))
            lines[i] = lines[i].upper() if rng.random() < .5 else lines[i].lower()
        elif op == 3:
            lines.insert(rng.randrange(len(lines) + 1), rng.choice(INJECT))
        elif op == 4:
            cut = rng.randrange(len("\n".join(lines)) + 1)
            lines = "\n".join(lines)[:cut].split("\n")
        else:
            lines = ["<html><body>"] + [f"<p>{line}</p>" for line in lines] + ["</body></html>"]
    return "\n".join(lines)


def test_fuzz_invariants(graph):
    rng = random.Random(1031)
    for n in range(400):
        text = mutate(rng.choice(SAMPLES), rng)
        case = analyse(text, client=None, graph=graph, checks=FAKE_CHECKS)
        v, inv = case.verdict, case.invoice
        ctx = f"mutation {n}: {text[:300]!r}"
        assert v.decision in ("PAY", "HOLD", "BLOCK"), ctx
        contradicted = [e for e in v.evidence if e.status == "contradicted"]
        if v.decision == "BLOCK":                                   # BLOCK needs proof (10.1 rule 5)
            assert contradicted, ctx
        if v.decision == "PAY":                                     # never auto-pay these (10.1, R3)
            assert not any(e.hard_block for e in contradicted), ctx
            assert not inv.bank_change_requested and inv.amount and inv.amount > 0, ctx
            assert v.confidence in ("HIGH", "MEDIUM") and v.risk_score < 25, ctx
        if not contradicted:
            assert v.risk_score == 0 and v.decision != "BLOCK", ctx  # absence is never fraud
        report = render(v, inv, case.ctx)
        for s in SECRETS:
            assert s not in report.replace(" ", ""), (s, ctx)


def test_fuzz_extract_never_raises():
    rng = random.Random(7)
    alphabet = "abcXYZ019 ,.:;@<>[]()$€£₹-/\n\t\"'"
    for _ in range(2000):
        extract("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 300))))
