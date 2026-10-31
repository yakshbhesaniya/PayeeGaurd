"""PayeeGuard web desk: a small JSON API + static frontend (stdlib only, no extra dependencies).

  python web.py            -> http://127.0.0.1:8787
  python web.py --reset    -> wipe the demo graph and re-seed it first
"""

import argparse
import json
import mimetypes
import sys
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from payeeguard import config, gate
from payeeguard.client import CrustdataClient
from payeeguard.extract import extract
from payeeguard.graph import Graph, seed
from payeeguard.pipeline import onboard_vendor, verify

WEB = config.ROOT / "web"
SAMPLES = config.ROOT / "samples"
MAX_BODY = 200_000
CLIENT = CrustdataClient()
LOCK = threading.Lock()   # one verification at a time keeps the SQLite graph and the demo story consistent


def _graph() -> Graph:
    return Graph()


def ensure_seeded(reset: bool = False) -> list[str]:
    db = config.data_dir() / "graph.db"
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
    g = _graph()
    try:
        if g.db.execute("SELECT 1 FROM edges WHERE rel='PAID' LIMIT 1").fetchone():
            return []
        return seed(g, CLIENT)
    finally:
        g.close()


def samples() -> dict:
    out = []
    for p in sorted(SAMPLES.glob("*.txt")):
        raw = p.read_text(encoding="utf-8")
        inv, meta = extract(raw)
        subject = next((ln.split(":", 1)[1].strip() for ln in raw.splitlines() if ln.lower().startswith("subject:")), p.stem)
        out.append({"id": p.stem, "subject": subject, "from": inv.sender_email, "from_name": meta.display_name,
                    "amount": inv.amount, "currency": inv.currency, "raw": raw})
    onb = json.loads((SAMPLES / "onboarding.json").read_text(encoding="utf-8"))["requests"]
    return {"emails": out, "onboarding": onb}


def api_verify(body: dict) -> dict:
    raw = str(body.get("raw_text") or "")[:MAX_BODY]
    extras: dict = {}
    g = _graph()
    try:
        v, md = verify(raw, client=CLIENT, graph=g, extras=extras)
        state = (g.get_case(v.case_id) or {}).get("state", v.decision)
    finally:
        g.close()
    return {"verdict": asdict(v), "markdown": md, "state": state, **extras}


def api_onboard(body: dict) -> dict:
    claims = {k: body[k] for k in ("claimed_country", "claimed_headcount", "claimed_offices") if body.get(k)}
    extras: dict = {}
    g = _graph()
    try:
        decision, md = onboard_vendor(str(body.get("vendor_name") or ""), str(body.get("website") or ""), claims,
                                      client=CLIENT, graph=g, extras=extras)
    finally:
        g.close()
    return {"decision": decision, "markdown": md, **extras}


def api_guard(body: dict) -> dict:
    """Pre-payment check for ANY AI payment agent over plain HTTP: allow or deny, with reasons."""
    raw = str(body.get("raw_text") or "")[:MAX_BODY]
    g = _graph()
    try:
        v, _ = verify(raw, client=CLIENT, graph=g)
    finally:
        g.close()
    reasons = [e.detail for e in sorted((e for e in v.evidence if e.status == "contradicted"),
                                        key=lambda e: (not e.hard_block, -e.weight))][:3]
    return {"allow": v.decision == "PAY", "decision": v.decision, "case_id": v.case_id, "risk": v.risk_score,
            "attack": v.hypothesis.attack_type if v.hypothesis else "none", "rule": v.decision_rule,
            "reasons": reasons, "pay_with": "/api/pay" if v.decision == "PAY" else None,
            "next_step": {"PAY": "Payment may be released through the gate.",
                          "HOLD": "Do not pay: a human must call the vendor on its independently sourced contact.",
                          "BLOCK": "Do not pay: fraud case file generated."}[v.decision]}


def api_stats(_body: dict | None = None) -> dict:
    g = _graph()
    try:
        cases = [c for c in g.all_cases() if not c["case_id"].startswith("seed")]
    finally:
        g.close()
    from payeeguard.scoring import to_usd
    by = {"PAY": 0, "HOLD": 0, "BLOCK": 0}
    stopped = paid = 0.0
    ms = []
    for c in cases:
        d = c["data"]
        by[c["decision"]] = by.get(c["decision"], 0) + 1
        usd = to_usd(d.get("amount"), d.get("currency")) or 0.0
        if c["state"] in ("BLOCK", "HOLD"):
            stopped += usd
        if c["state"] == "PAID_SIMULATED":
            paid += usd
        if d.get("ms") is not None:
            ms.append(d["ms"])
    return {"cases": len(cases), "by_decision": by, "stopped_usd": round(stopped, 2), "paid_usd": round(paid, 2),
            "avg_ms": round(sum(ms) / len(ms)) if ms else None, "credits": round(CLIENT.credits, 1),
            "live_calls": CLIENT.calls}


def api_screen(body: dict) -> dict:
    from payeeguard.screening import parse_csv, screen
    text = body.get("csv") if body.get("csv") else (SAMPLES / "vendor_master.csv").read_text(encoding="utf-8")
    rows = parse_csv(str(text)[:MAX_BODY])
    if not rows:
        return {"error_msg": "No vendor rows found. Expected a header: name,domain,iban,account,swift,ifsc,country",
                "vendors": [], "counts": {}, "total": 0}
    g = _graph()
    try:
        return screen(rows[:200], CLIENT, g)
    finally:
        g.close()


def api_gate(kind: str, body: dict) -> dict:
    g = _graph()
    try:
        cid = str(body.get("case_id") or "")
        if kind == "pay":
            msg = gate.pay_invoice(cid, g, CLIENT)
        else:
            msg = gate.resolve_case(cid, str(body.get("decision") or ""), str(body.get("reviewer") or "").strip(),
                                    str(body.get("note") or "").strip(), g)
        case = g.get_case(cid)
        return {"message": msg, "state": case["state"] if case else None,
                "ok": not msg.startswith(("Refused", "Unknown", "decision must", "reviewer and note"))}
    finally:
        g.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "PayeeGuard"

    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, code: int, payload: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/samples":
            return self._json(200, samples())
        if path == "/api/stats":
            return self._json(200, api_stats())
        if path.startswith("/api/case/"):
            g = _graph()
            try:
                return self._json(200, {"message": gate.case_status(path.rsplit("/", 1)[1], g)})
            finally:
                g.close()
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (WEB / rel).resolve()
        if WEB.resolve() not in f.parents and f != WEB.resolve() or not f.is_file():   # no path traversal
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "JSON object expected"})
        routes = {"/api/verify": api_verify, "/api/onboard": api_onboard, "/api/guard": api_guard,
                  "/api/screen": api_screen,
                  "/api/pay": lambda b: api_gate("pay", b), "/api/resolve": lambda b: api_gate("resolve", b)}
        if self.path == "/api/reset":
            with LOCK:
                lines = ensure_seeded(reset=True)
            return self._json(200, {"message": "Demo graph reset", "seeded": lines})
        fn = routes.get(self.path)
        if not fn:
            return self._json(404, {"error": "unknown route"})
        try:
            with LOCK:
                return self._json(200, fn(body))
        except Exception as e:  # noqa: BLE001 - never leak a stack trace to the page
            sys.stderr.write(f"  error on {self.path}: {type(e).__name__}: {e}\n")
            return self._json(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    config.setup_logging()
    for line in ensure_seeded(a.reset):
        print("seeded:", line)
    print(f"PayeeGuard desk on http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
