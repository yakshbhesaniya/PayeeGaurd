"""Spec 13.5 S10, 13.7 (M1-M7) and 13.8 (Y1-Y4): pipeline isolation, server, CLI, privacy."""
import asyncio
import glob
import json
import os
import subprocess
import sys

from fakes import FAKE_CHECKS, ROOT, sample

from payeeguard import config
from payeeguard.models import Evidence
from payeeguard.pipeline import analyse, finish, run_all
from payeeguard.report import chat_report, redact


def test_check_crash_isolated(graph):
    """S10: one check raising becomes unchecked/api_error; the verdict still returns."""
    def boom(*a):
        raise RuntimeError("secret detail")
    v = analyse(sample("01_legit.txt"), client=None, graph=graph,
                checks=FAKE_CHECKS + [("check_boom", "SIGNATORY", boom)]).verdict
    row = [e for e in v.evidence if e.check == "check_boom"][0]
    assert row.status == "unchecked" and row.unchecked_reason == "api_error" and "secret detail" not in row.detail
    assert v.decision == "PAY"


def test_graph_failure_does_not_crash():
    class Broken:
        def __getattr__(self, name):
            def f(*a, **k):
                raise OSError("db locked")
            return f
    v = analyse(sample("01_legit.txt"), client=None, graph=Broken(), checks=[]).verdict
    assert v.decision == "HOLD"


def test_no_checks_installed_fail_safe():
    v = analyse(sample("01_legit.txt"), client=None, graph=None).verdict
    assert v.decision == "HOLD" and v.confidence == "LOW"


def test_server_raw_only(graph, monkeypatch):
    """M1: no fields, only raw_text."""
    import server
    monkeypatch.setattr("payeeguard.wiring.load_checks", lambda: FAKE_CHECKS)
    monkeypatch.setattr("payeeguard.wiring.open_graph", lambda path=None: graph)
    monkeypatch.setattr("payeeguard.wiring.make_client", lambda: None)
    out = server.verify_payee(sample("01_legit.txt"))
    assert out.startswith("# PayeeGuard verdict: PAY")


def test_server_x1_through_tool(graph, monkeypatch):
    """M2 through the MCP tool: the injected sender is overruled and shown."""
    import server
    monkeypatch.setattr("payeeguard.wiring.load_checks", lambda: FAKE_CHECKS)
    monkeypatch.setattr("payeeguard.wiring.open_graph", lambda path=None: graph)
    monkeypatch.setattr("payeeguard.wiring.make_client", lambda: None)
    out = server.verify_payee(sample("08_prompt_injection.txt"), sender_email="billing@freshworks.com")
    assert "verdict: BLOCK" in out and "X1_extraction" in out


def test_server_tools_registered_with_schema():
    import server
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {"verify_payee", "onboard_vendor", "pay_invoice", "resolve_case", "case_status"}
    props = tools["verify_payee"].inputSchema["properties"]
    assert "raw_text" in props and "bank_iban" in props
    assert tools["verify_payee"].inputSchema["required"] == ["raw_text"]


def test_server_tool_error_is_failsafe(monkeypatch):
    import server
    monkeypatch.setattr("server.analyse", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    assert "Do not pay" in server.verify_payee("anything")


def test_paths_repo_relative(tmp_path):
    """M3: start from another directory; .env/policy resolve from the repo root."""
    code = "import server, payeeguard.config as c; print(c.ROOT); print(c.policy()['decision']['auto_pay_limit_usd'])"
    out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, r'{ROOT}'); {code}"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert str(ROOT) in out.stdout and "5000" in out.stdout


def test_utf8_output(tmp_path):
    """M4: CLI prints non-ASCII (₹, ✔, ••) on a cp1252 console without crashing."""
    f = tmp_path / "in.txt"
    f.write_text("From: Opéra <billing@opera.example>\n\nInvoice INV-1 for ₹3,45,000 due", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    out = subprocess.run([sys.executable, str(ROOT / "cli.py"), "verify", str(f)], capture_output=True, env=env,
                         timeout=60)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert "INR 345,000.00" in out.stdout.decode("utf-8")


def test_report_truncation(graph):
    """M5: chat shows the top 12 rows and the file path; the file has the full ledger."""
    many = [Evidence(f"Z{i}_x", "CONTEXT", "c", "verified", "d" * 400, "s") for i in range(40)]
    c = analyse(sample("01_legit.txt"), client=None, graph=graph, checks=FAKE_CHECKS + [("m", "CONTEXT", lambda *a: many)])
    finish(c)
    text = chat_report(c.verdict, c.invoice, c.ctx)
    assert len(text) <= 15_000 and "report.md" in text
    full = open(os.path.join(os.environ["PAYEEGUARD_REPORTS_DIR"], c.verdict.case_id, "report.md"), encoding="utf-8").read()
    assert full.count("| Z") == 40


def test_cache_atomic_write(graph):
    """M6: case records are replaced atomically (no temp files left behind)."""
    for _ in range(3):
        run_all(sample("01_legit.txt"), client=None, graph=graph, checks=FAKE_CHECKS)
    cases = os.path.join(os.environ["PAYEEGUARD_DATA_DIR"], "cases")
    assert not glob.glob(os.path.join(cases, ".tmp-*")) and len(os.listdir(cases)) == 1


def test_policy_defaults(tmp_path):
    """M7: missing or invalid policy.toml -> built-in defaults."""
    assert config.load_policy(tmp_path / "missing.toml")["decision"]["block_risk"] == 60
    bad = tmp_path / "bad.toml"
    bad.write_text("[decision\nblock_risk = ", encoding="utf-8")
    assert config.load_policy(bad)["decision"]["hold_risk"] == 25
    partial = tmp_path / "p.toml"
    partial.write_text("[decision]\nblock_risk = 70\n", encoding="utf-8")
    p = config.load_policy(partial)
    assert p["decision"]["block_risk"] == 70 and p["decision"]["hold_risk"] == 25


def test_no_raw_text_persisted(graph):
    """Y1: reports/ and data/ never hold the email body."""
    for name in ("03_gmail_signatory.txt", "06_hero_freshworks_mule.txt", "07_unknown_vendor.txt"):
        run_all(sample(name), client=None, graph=graph, checks=FAKE_CHECKS)
    blobs = _all_outputs()
    for phrase in ("Using personal email as our server is down", "regional treasury setup", "IT consulting services"):
        assert phrase not in blobs


def test_no_full_account_in_outputs(graph):
    """Y2: only country + last 4 anywhere in reports, case records, outbox."""
    from payeeguard import gate
    for name in sorted(glob.glob(str(ROOT / "samples" / "*.txt"))):
        v = run_all(open(name, encoding="utf-8").read(), client=None, graph=graph, checks=FAKE_CHECKS)
        gate.pay_invoice(v.case_id, graph=graph)
    blobs = _all_outputs()
    for secret in ("000123456789", "12345698765432", "GB82WEST", "GB82 WEST", "GB33BUKB", "DE89370400440532013000",
                   "DE89 3704", "123405000789"):
        assert secret not in blobs, secret


def test_artifacts_labelled(graph):
    """Y4: every outbound artifact carries SIMULATED / DRAFT."""
    for name in ("01_legit.txt", "04_new_account_real_domain.txt", "02_lookalike.txt", "07_unknown_vendor.txt"):
        v = run_all(sample(name), client=None, graph=graph, checks=FAKE_CHECKS)
        for a in v.actions:
            text = open(a.path, encoding="utf-8").read()
            assert a.kind == "payment_instruction" and '"simulated": true' in text or "SIMULATED / DRAFT" in text


def test_redact_generic_iban():
    assert redact("pay GB82 WEST 1234 5698 7654 32 now") == "pay ••5432 now"
    assert redact("crustdata:person/123456789") == "crustdata:person/123456789"


def test_verdict_json_masks(graph):
    from payeeguard.report import verdict_json
    c = analyse(sample("05_zoho_lookalike_mule.txt"), client=None, graph=graph, checks=FAKE_CHECKS)
    blob = json.dumps(verdict_json(c.verdict, c.invoice), ensure_ascii=False)
    assert "GB82WEST12345698765432" not in blob and "GB ••5432" in blob


def test_eml_intake(tmp_path):
    from cli import read_input
    eml = tmp_path / "m.eml"
    eml.write_bytes(b"From: Billing <billing@freshworks.com>\r\nReply-To: x@evil.com\r\nSubject: Invoice INV-1\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n\r\nPay USD 10.00\r\n")
    text = read_input(str(eml))
    assert "Reply-To: x@evil.com" in text and "Pay USD 10.00" in text


def _all_outputs() -> str:
    out = []
    for env in ("PAYEEGUARD_REPORTS_DIR", "PAYEEGUARD_DATA_DIR", "PAYEEGUARD_PAYMENTS_DIR"):
        for p in glob.glob(os.path.join(os.environ[env], "**", "*"), recursive=True):
            if os.path.isfile(p):
                out.append(open(p, encoding="utf-8", errors="replace").read())
    return "\n".join(out)
