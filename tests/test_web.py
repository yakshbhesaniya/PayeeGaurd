"""The web desk end to end: real HTTP server in-process, mock Crustdata transport, temp data dir."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import web
from conftest import email, seed_freshworks
from payeeguard.graph import Graph


@pytest.fixture
def server(env, client, monkeypatch):
    monkeypatch.setattr(web, "CLIENT", client)
    g = Graph()                      # data dir comes from the env fixture
    seed_freshworks(g)
    g.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(base, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_static_and_samples(server):
    assert call(server, "/")[0] == 200
    assert call(server, "/app.css")[0] == 200
    code, body = call(server, "/api/samples")
    data = json.loads(body)
    assert code == 200 and len(data["emails"]) == 10 and len(data["onboarding"]) == 3


@pytest.mark.parametrize("path", ["/../.env", "/%2e%2e/.env", "/..%5c.env", "/payeeguard/config.py", "/nope.js"])
def test_no_path_traversal(server, path):
    assert call(server, path)[0] == 404


@pytest.mark.parametrize("kind,code", [("bad_json", 400), ("array", 400), ("too_large", 413)])
def test_malformed_bodies(server, kind, code):
    raw = {"bad_json": b"{oops", "array": b"[1,2]", "too_large": b"x" * 250_000}[kind]
    assert call(server, "/api/verify", raw=raw)[0] == code


def test_unknown_route(server):
    assert call(server, "/api/nope", {})[0] == 404


def test_verify_pay_flow(server):
    code, body = call(server, "/api/verify", {"raw_text": email(body="Pay to account on file.")})
    d = json.loads(body)
    assert code == 200 and d["verdict"]["decision"] == "PAY" and d["facts"] == {"who": True, "channel": True, "money": True}
    cid = d["verdict"]["case_id"]
    r = json.loads(call(server, "/api/pay", {"case_id": cid})[1])
    assert r["ok"] and r["state"] == "RELEASED"
    r = json.loads(call(server, "/api/pay", {"case_id": cid})[1])
    assert not r["ok"] and "already paid" in r["message"]


def test_hold_needs_approval_over_http(server):
    d = json.loads(call(server, "/api/verify", {"raw_text": email(body="Remit to IBAN GB82 WEST 1234 5698 7654 32")})[1])
    cid = d["verdict"]["case_id"]
    assert d["verdict"]["decision"] == "HOLD"
    assert not json.loads(call(server, "/api/pay", {"case_id": cid})[1])["ok"]
    r = json.loads(call(server, "/api/resolve", {"case_id": cid, "decision": "approve", "reviewer": "Asha"})[1])
    assert not r["ok"]                                       # note is required
    r = json.loads(call(server, "/api/resolve", {"case_id": cid, "decision": "approve", "reviewer": "Asha",
                                                 "note": "called official number"})[1])
    assert r["ok"] and r["state"] == "APPROVED"
    assert json.loads(call(server, "/api/pay", {"case_id": cid})[1])["ok"]


def test_verify_odd_payloads_never_500(server):
    for body in ({}, {"raw_text": 12345}, {"raw_text": None}, {"raw_text": "<script>alert(1)</script>"}):
        assert call(server, "/api/verify", body)[0] == 200


def test_onboard_route(server):
    d = json.loads(call(server, "/api/onboard", {"vendor_name": "Freshworks", "website": "https://www.freshworks.com"})[1])
    assert d["decision"] == "APPROVE" and d["leaders"]
    assert call(server, "/api/onboard", {})[0] == 200
