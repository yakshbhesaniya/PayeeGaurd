"""PayeeGuard CLI (no LLM needed).

  python cli.py seed                         load samples/seed_history.json into the graph
  python cli.py verify samples/06_hero_freshworks_mule.txt
  python cli.py pay <case_id>
  python cli.py resolve <case_id> approve|reject|override --by NAME --note TEXT
  python cli.py status <case_id>
  python cli.py onboard "Freshworks" https://www.freshworks.com [--country US] [--headcount N] [--offices N]
  python cli.py eval                         run all samples on a fresh graph and compare with expected.json
  python cli.py audit                        verify the audit hash chain
"""

import argparse
import json
import os
import sys
from pathlib import Path

from payeeguard import audit, config, gate
from payeeguard.client import CrustdataClient
from payeeguard.graph import Graph, seed
from payeeguard.pipeline import onboard_vendor, verify

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # edge case M4: Windows console


def cmd_eval(args) -> int:
    samples = config.ROOT / "samples"
    expected = json.loads((samples / "expected.json").read_text(encoding="utf-8"))
    db = config.data_dir() / "eval_graph.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    graph, client = Graph(db), CrustdataClient()
    seed(graph, client)
    rows, ok = [], 0
    for name, exp in expected.items():
        if name.startswith("_"):
            continue
        saved = {k: os.environ.get(k) for k in exp.get("env", {})}
        os.environ.update(exp.get("env", {}))
        try:
            v, _ = verify((samples / name).read_text(encoding="utf-8"), client=CrustdataClient(), graph=graph,
                          save=args.save)
        finally:
            for k, val in saved.items():
                os.environ.pop(k, None) if val is None else os.environ.__setitem__(k, val)
        attack = v.hypothesis.attack_type if v.hypothesis else "none"
        good = v.decision == exp["decision"] and attack == exp["attack_type"]
        ok += good
        rows.append((name, exp["decision"], v.decision, exp["attack_type"], attack, v.risk_score, v.confidence,
                     v.decision_rule, "✅" if good else "❌"))
    onb = json.loads((samples / "onboarding.json").read_text(encoding="utf-8"))["requests"]
    for r in onb:
        claims = {k: r[k] for k in ("claimed_country", "claimed_headcount", "claimed_offices") if k in r}
        dec, _ = onboard_vendor(r["vendor_name"], r["website"], claims, client=client, graph=graph)
        good = dec == r["expected"]
        ok += good
        rows.append((r["id"], r["expected"], dec, "-", "-", "-", "-", "Mode 2 onboarding", "✅" if good else "❌"))
    graph.close()
    print(f"{'sample':32} {'expect':7} {'got':7} {'expected attack':28} {'got attack':28} risk conf  rule")
    for r in rows:
        print(f"{r[0][:32]:32} {r[1]:7} {r[2]:7} {r[3][:28]:28} {r[4][:28]:28} {str(r[5]):>4} {r[6]:6} {r[8]} {r[7]}")
    print(f"\n{ok}/{len(rows)} passed")
    return 0 if ok == len(rows) else 1


def _fresh_eval_graph(client) -> Graph:
    db = config.data_dir() / "eval_graph.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    g = Graph(db)
    seed(g, client)
    return g


def _invoice_samples():
    samples = config.ROOT / "samples"
    expected = json.loads((samples / "expected.json").read_text(encoding="utf-8"))
    return samples, [(n, e) for n, e in expected.items() if not n.startswith("_")]


def cmd_ablate(args) -> int:
    """For each sample: which single check, if removed, would change the decision?"""
    from payeeguard.ablation import ablate
    from payeeguard.extract import extract

    client = CrustdataClient()
    graph = _fresh_eval_graph(client)
    samples, items = _invoice_samples()
    print(f"{'sample':32} {'verdict':7}  decisive checks (decision if removed)")
    for name, exp in items:
        if exp.get("env"):
            continue
        raw = (samples / name).read_text(encoding="utf-8")
        extras: dict = {}
        v, _ = verify(raw, client=client, graph=graph, save=False, extras=extras)
        inv, meta = extract(raw)
        known = any(e.check == "G1_account_history" and e.unchecked_reason != "not_applicable" for e in v.evidence)
        flips = [f"{c}→{d}" for c, d, a in ablate(v, inv, known, meta.credit_note) if d != v.decision]
        print(f"{name[:32]:32} {v.decision:7}  {', '.join(flips) or '(no single check decides: verdict is robust)'}")
    graph.close()
    return 0


def _variants(raw: str) -> dict[str, str]:
    """Rewrites that must NOT change the verdict (metamorphic testing)."""
    head, _, body = raw.partition("\n\n")
    return {
        "crlf": raw.replace("\n", "\r\n"),
        "html": "<html><body><p>" + raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>") + "</p></body></html>",
        "forwarded_by_colleague": "From: AP Clerk <clerk@ourco.example>\nSubject: Fwd: please check\n\nCan you verify this?\n\n"
                                  "---------- Forwarded message ---------\n" + raw,
        "quoted_history": raw + "\n\nOn Mon, 2 Oct 2026 Someone wrote:\n> Please pay USD 99,999 to IBAN DE89 3704 0044 0532 0130 00\n",
        "extra_whitespace": raw.replace("\n", "\n\n").replace(": ", ":   "),
        "trailing_noise": raw + "\n\n--\nSent from my phone. This email and any attachments are confidential.\n",
    }


def cmd_stress(args) -> int:
    client = CrustdataClient()
    samples, items = _invoice_samples()
    items = [(n, e) for n, e in items if not e.get("env")]
    raws = [(samples / n).read_text(encoding="utf-8") for n, _ in items]

    def run_after(i: int, text: str):
        """Fresh graph, replay samples 0..i-1 (same history the original saw), then verify text."""
        g = _fresh_eval_graph(client)
        try:
            for earlier in raws[:i]:
                verify(earlier, client=client, graph=g, save=False)
            v, _ = verify(text, client=client, graph=g, save=False)
            return v.decision, (v.hypothesis.attack_type if v.hypothesis else "none"), v.decision_rule
        finally:
            g.close()

    fails, total = [], 0
    for i, (name, _) in enumerate(items):
        base = run_after(i, raws[i])
        for vname, text in _variants(raws[i]).items():
            total += 1
            got = run_after(i, text)
            if got[:2] != base[:2]:
                fails.append(f"{name} [{vname}]: {base[0]}/{base[1]} -> {got[0]}/{got[1]} ({got[2]})")
    for f in fails:
        print("❌", f)
    print(f"\n{total - len(fails)}/{total} variants kept the same verdict and attack type")
    return 0 if not fails else 1


def main(argv=None) -> int:
    config.setup_logging()
    p = argparse.ArgumentParser(prog="payeeguard", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    v = sub.add_parser("verify")
    v.add_argument("file")
    v.add_argument("--json", action="store_true")
    sub.add_parser("pay").add_argument("case_id")
    st = sub.add_parser("status")
    st.add_argument("case_id")
    r = sub.add_parser("resolve")
    r.add_argument("case_id")
    r.add_argument("decision", choices=["approve", "reject", "override"])
    r.add_argument("--by", required=True)
    r.add_argument("--note", required=True)
    o = sub.add_parser("onboard")
    o.add_argument("name")
    o.add_argument("website")
    o.add_argument("--country")
    o.add_argument("--headcount", type=int)
    o.add_argument("--offices", type=int)
    e = sub.add_parser("eval")
    e.add_argument("--save", action="store_true", help="also write reports for each sample")
    sub.add_parser("audit")
    sub.add_parser("ablate", help="which check decides each sample's verdict")
    sub.add_parser("stress", help="metamorphic rewrites of every sample must keep the same verdict")
    a = p.parse_args(argv)

    if a.cmd == "eval":
        return cmd_eval(a)
    if a.cmd == "ablate":
        return cmd_ablate(a)
    if a.cmd == "stress":
        return cmd_stress(a)
    if a.cmd == "audit":
        ok, msg = audit.verify()
        print(msg)
        return 0 if ok else 1
    graph = Graph()
    try:
        if a.cmd == "seed":
            for line in seed(graph, CrustdataClient()):
                print("seeded:", line)
        elif a.cmd == "verify":
            verdict, md = verify(Path(a.file).read_text(encoding="utf-8"), graph=graph)
            print(json.dumps({"decision": verdict.decision, "case_id": verdict.case_id}) if a.json else md)
        elif a.cmd == "pay":
            print(gate.pay_invoice(a.case_id, graph, CrustdataClient()))
        elif a.cmd == "status":
            print(gate.case_status(a.case_id, graph))
        elif a.cmd == "resolve":
            print(gate.resolve_case(a.case_id, a.decision, a.by, a.note, graph))
        elif a.cmd == "onboard":
            claims = {k: v for k, v in (("claimed_country", a.country), ("claimed_headcount", a.headcount),
                                        ("claimed_offices", a.offices)) if v}
            print(onboard_vendor(a.name, a.website, claims, graph=graph)[1])
    finally:
        graph.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
