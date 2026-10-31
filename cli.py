"""PayeeGuard CLI (no LLM): verify · pay · resolve · status · onboard · eval · ablate · seed · audit verify."""
from __future__ import annotations

import argparse
import email
import email.policy
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from payeeguard import gate, wiring
from payeeguard.config import ROOT, data_dir, reports_dir
from payeeguard.pipeline import Case, analyse, finish, onboard
from payeeguard.report import chat_report

SAMPLES = ROOT / "samples"


def read_input(path: str) -> str:
    """Text file, '-' for stdin, or a .eml file (headers incl. Reply-To / Return-Path + text body)."""
    if path == "-":
        return sys.stdin.read()
    p = Path(path)
    if p.suffix.lower() != ".eml":
        return p.read_text(encoding="utf-8", errors="replace")
    msg = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
    head = [f"{h}: {msg[h]}" for h in ("From", "Reply-To", "Return-Path", "Subject", "Date") if msg[h]]
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part is not None else ""
    return "\n".join(head) + "\n\n" + body


@contextmanager
def env(overrides: dict[str, str]) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def cmd_verify(a: argparse.Namespace) -> int:
    try:
        text = read_input(a.file)
    except OSError as e:
        print(f"cannot read {a.file}: {e.strerror or e}", file=sys.stderr)
        return 2
    case = analyse(text)
    finish(case)
    if a.json:
        from payeeguard.report import verdict_json
        print(json.dumps(verdict_json(case.verdict, case.invoice), indent=2, ensure_ascii=False))
    else:
        print(chat_report(case.verdict, case.invoice, case.ctx))
    return 0


def run_samples(names: list[str] | None = None) -> tuple[dict[str, Case], dict[str, Any]]:
    """Fresh eval graph seeded from seed_history.json; samples in name order (05 before 06)."""
    expected = json.loads((SAMPLES / "expected.json").read_text(encoding="utf-8"))
    eval_dir = data_dir() / "eval"
    shutil.rmtree(eval_dir, ignore_errors=True)
    eval_dir.mkdir(parents=True)
    db = data_dir() / "eval_graph.db"
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        p.unlink(missing_ok=True)
    graph = wiring.open_graph(str(db))
    with env({"PAYEEGUARD_DATA_DIR": str(eval_dir), "PAYEEGUARD_REPORTS_DIR": str(reports_dir() / "eval")}):
        print(wiring.seed_graph(graph, str(SAMPLES / "seed_history.json"), wiring.make_client()), file=sys.stderr)
        cases: dict[str, Case] = {}
        for name in sorted(k for k in expected if not k.startswith("_")):
            if names and name not in names:
                continue
            with env(expected[name].get("env", {})):
                case = analyse((SAMPLES / name).read_text(encoding="utf-8"), client=wiring.make_client(), graph=graph)
                finish(case)
            cases[name] = case
    return cases, expected


def cmd_eval(a: argparse.Namespace) -> int:
    if not wiring.checks_available():
        print("note: check modules (A/B) are not merged yet; verdicts are fail-safe only.", file=sys.stderr)
    cases, expected = run_samples()
    ok = 0
    print("| Sample | Expected | Got | Expected attack | Got attack | Rule | OK |\n|---|---|---|---|---|---|---|")
    for name, case in cases.items():
        v, exp = case.verdict, expected[name]
        got_attack = v.hypothesis.attack_type if v.hypothesis else "none"
        good = v.decision == exp["decision"] and got_attack == exp["attack_type"]
        ok += good
        print(f"| {name} | {exp['decision']} | {v.decision} | {exp['attack_type']} | {got_attack} | "
              f"{v.decision_rule} | {'✅' if good else '❌'} |")
    print(f"\n{ok}/{len(cases)} samples match.")
    onb = json.loads((SAMPLES / "onboarding.json").read_text(encoding="utf-8"))
    onb_ok = 0
    for r in onb["requests"]:
        res = onboard(r["vendor_name"], r["website"], r.get("claimed_country"), r.get("claimed_headcount"),
                      r.get("claimed_offices"))
        onb_ok += res["decision"] == r["expected"]
        print(f"onboarding {r['id']}: expected {r['expected']}, got {res['decision']} ({res['reason']})")
    print(f"{onb_ok}/{len(onb['requests'])} onboarding requests match.")
    return 0 if ok == len(cases) and onb_ok == len(onb["requests"]) else 1


def cmd_ablate(a: argparse.Namespace) -> int:
    from payeeguard.ablation import ablation_markdown
    cases, _ = run_samples()
    text = ablation_markdown(cases)
    out = reports_dir() / "ablation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text + f"\nwritten to {out}")
    return 0


def cmd_onboard(a: argparse.Namespace) -> int:
    from payeeguard.report import render_onboarding
    res = onboard(a.name, a.website, a.country, a.headcount, a.offices)
    print(render_onboarding(res))
    return 0


def cmd_seed(a: argparse.Namespace) -> int:
    try:
        print(wiring.seed_graph(wiring.open_graph(), str(SAMPLES / "seed_history.json"), wiring.make_client()))
    except Exception as e:  # noqa: BLE001 - G-9: clear message, no traceback
        print(f"seed failed: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_audit(a: argparse.Namespace) -> int:
    mod = wiring._module("audit")
    fn = getattr(mod, "verify", None) if mod else None
    if fn is None:
        print("audit.py not merged yet", file=sys.stderr)
        return 1
    print(fn())
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # M4: Windows console
        sys.stderr.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="payeeguard")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify an invoice / bank-change email (.txt, .eml or - for stdin)")
    v.add_argument("file")
    v.add_argument("--json", action="store_true")
    pay = sub.add_parser("pay", help="pay a case through the gate (SIMULATED)")
    pay.add_argument("case")
    r = sub.add_parser("resolve", help="human decision on a case")
    r.add_argument("case")
    r.add_argument("decision", choices=["approve", "reject", "override"])
    r.add_argument("--by", required=True)
    r.add_argument("--note", default="")
    s = sub.add_parser("status")
    s.add_argument("case")
    o = sub.add_parser("onboard", help="Mode 2 vendor onboarding / KYB")
    o.add_argument("name")
    o.add_argument("website")
    o.add_argument("--country")
    o.add_argument("--headcount", type=int)
    o.add_argument("--offices", type=int)
    sub.add_parser("eval")
    sub.add_parser("ablate")
    sub.add_parser("seed")
    au = sub.add_parser("audit")
    au.add_argument("action", choices=["verify"])
    a = p.parse_args(argv)

    if a.cmd == "pay":
        print(gate.pay_invoice(a.case))
        return 0
    if a.cmd == "resolve":
        print(gate.resolve_case(a.case, a.decision, a.by, a.note))
        return 0
    if a.cmd == "status":
        print(gate.case_status(a.case))
        return 0
    return {"verify": cmd_verify, "eval": cmd_eval, "ablate": cmd_ablate, "onboard": cmd_onboard,
            "seed": cmd_seed, "audit": cmd_audit}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
