"""Ablation (spec 3, 19b): re-decide every sample with each check's evidence removed, to show what decides each case.

Re-scoring reuses the evidence from one run, so it costs no extra Crustdata calls and is deterministic.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from .pipeline import Case, assess


def check_key(check: str) -> str:
    """'G3_account_flagged' -> 'G3'."""
    return check.split("_", 1)[0]


def ablate(cases: dict[str, Case]) -> tuple[list[str], dict[str, dict[str, str]]]:
    keys = sorted({check_key(e.check) for c in cases.values() for e in c.verdict.evidence})
    table: dict[str, dict[str, str]] = {}
    for name, case in cases.items():
        row = {"full": case.verdict.decision}
        for k in keys:
            kept = [e for e in case.verdict.evidence if check_key(e.check) != k]
            if len(kept) == len(case.verdict.evidence):
                row[k] = ""
                continue
            ctx: dict[str, Any] = dict(case.ctx)
            v = assess(replace(case.invoice), kept, ctx)
            row[k] = v.decision if v.decision != case.verdict.decision else "·"
        table[name] = row
    return keys, table


def ablation_markdown(cases: dict[str, Case]) -> str:
    keys, table = ablate(cases)
    out = ["# Ablation: decision with one check removed", "",
           "`·` = check present, decision unchanged · blank = check did not fire · a decision = that check decides the case", "",
           "| " + " | ".join(["Sample", "Full", *keys]) + " |", "|" + "---|" * (len(keys) + 2)]
    for name, row in table.items():
        out.append("| " + " | ".join([name, f"**{row['full']}**", *(row[k] for k in keys)]) + " |")
    return "\n".join(out) + "\n"
