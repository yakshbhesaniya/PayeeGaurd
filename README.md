# PayeeGuard

**The trust check every human or AI accounts-payable agent runs before money moves.**

PayeeGuard takes an invoice or bank-change email it has never seen, finds out who is really involved (Crustdata company and people graph), follows the money (our payment-trust graph), names the most likely attack, and finishes the job:

- **PAY:** a simulated payment through a payment gate.
- **HOLD:** a callback script and a verification email to an *independently sourced* contact, plus an exception ticket.
- **BLOCK:** a complaint-ready fraud case file that links related attempts into one campaign.

> *"Every identity check passed. The money trail didn't."*

Everything outbound is **SIMULATED / DRAFT**. Nothing contacts anyone or moves money.

---

## Quick start

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install httpx "mcp<2" python-dotenv idna pytest ruff
cp .env.example .env            # add CRUSTDATA_API_KEY; set PAYEEGUARD_FP_SECRET (openssl rand -hex 32)

.venv/Scripts/python cli.py seed                                  # load samples/seed_history.json
.venv/Scripts/python cli.py verify samples/06_hero_freshworks_mule.txt
.venv/Scripts/python cli.py eval                                  # 10 samples + 3 onboarding requests
.venv/Scripts/python cli.py ablate                                # which evidence decides each case
.venv/Scripts/python -m pytest -q                                 # offline, no network
```

`mcp` must be **< 2**: version 2 renamed `FastMCP`.

### CLI

| Command | What it does |
|---|---|
| `verify FILE` | Verify a `.txt`, a `.eml` (parses From / Reply-To / Return-Path) or `-` for stdin. `--json` for machine output |
| `pay CASE` | Payment gate: SIMULATED payment, only for PAY or human-approved HOLD |
| `resolve CASE approve\|reject\|override --by NAME --note ...` | Human decision. `override` reopens a wrong BLOCK (note required, audited) |
| `status CASE` | Case state, rule, approvals |
| `onboard NAME WEBSITE [--country --headcount --offices]` | Mode 2 vendor onboarding (KYB): APPROVE / REVIEW / REJECT |
| `eval` · `ablate` · `seed` · `audit verify` | Evaluation on a fresh seeded graph, ablation table, graph seed, audit-chain check |

### Claude (MCP)

- **Claude Desktop (Windows):** edit `%APPDATA%\Claude\claude_desktop_config.json`, then fully quit and reopen Claude Desktop:
  ```json
  { "mcpServers": { "payeeguard": {
      "command": "D:\\Projects\\PayeeGaurd\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\PayeeGaurd\\server.py"] } } }
  ```
- **Claude Code:** `claude mcp add payeeguard -- .venv/Scripts/python.exe server.py`
- **Crustdata MCP** (follow-up research only, never changes a verdict): `claude mcp add --transport http crustdata https://install.crustdata.com/mcp`

Tools: `verify_payee(raw_text, …optional fields)`, `onboard_vendor`, `pay_invoice`, `resolve_case`, `case_status`. The server loads `.env`, `policy.toml`, `data/` and `reports/` relative to the repo, so Claude can start it from any directory.

---

## How it decides

```
raw email ─▶ extract.py (regex; Claude's fields only cross-checked: X1)
          ─▶ checks: company C1/C4/C5/C6 · channel C2/C2b/C8/C10 · signatory C3 · payment G1–G5/C9/C11 · behaviour C14
          ─▶ scoring.py: risk = Σ family maxima (BEHAVIOUR summed, cap 25) · confidence = who + channel + money
          ─▶ rules R1–R7 (first match wins, recorded) ─▶ investigator.py: named attack + ruled-out list
          ─▶ actions.py (finished work) · report.py (Markdown + JSON + Mermaid money trail) · gate.py (pay / resolve)
```

**Rules:** only *positive contradictions* add risk; a check that can't run is `unchecked`, never `contradicted`. Low confidence gives HOLD, never BLOCK. BLOCK needs a hard fact or risk ≥ 60. A new payment account is never auto-paid. Decisions are deterministic Python, with no LLM in the decision path.

| Rule | Condition | Decision |
|---|---|---|
| R1 | any hard fact (lookalike domain, account tied to a blocked case, duplicate to another account) | BLOCK |
| R2 | risk ≥ 60 | BLOCK |
| R3 | bank change, new account for a known vendor, two accounts, amount missing / ≤ 0 / credit note | HOLD |
| R4 | risk ≥ 25 | HOLD |
| R5 | confidence HIGH | PAY |
| R6 | confidence MEDIUM and amount ≤ auto-pay limit | PAY |
| R7 | otherwise | HOLD (+ KYB task when the vendor isn't in Crustdata) |

---

## The service: who pays for this today, and why we picked it

AP clerks, vendor-master teams, payment-fraud analysts and audit firms are paid to verify payees by hand. Two in five businesses have been hit by invoice fraud, averaging **$367,000 per incident**, and only a quarter have automated defences ([Ivalua, 2026](https://www.ivalua.com/press-releases/two-in-five-businesses-fall-victim-to-invoice-fraud-with-losses-averaging-367000/)). AI agents are starting to pay invoices with no one checking who the payee is. Banking checks (Confirmation of Payee, penny drop) confirm that an account belongs to *a* name. PayeeGuard checks whether the **request** is legitimate.

## The approaches we weighed

1. **LLM decides:** rejected. It isn't reproducible, and it can be prompt-injected (see sample 08).
2. **Keyword/rules checklist** (our v1/v2): rejected. It double-counted correlated signals, treated missing data as fraud, and missed real-mailbox attacks.
3. **Separate LLM agent per check:** rejected on cost and non-determinism.
4. **Chosen: identity graph (Crustdata) + money-trail graph + deterministic investigator.** Claude reads and orchestrates; Python decides.

## What failed (and what we changed)

- Person Identify is 403 on our key → C3 goes straight to Person Search.
- `/permissions` returns 404; Job Search has no company-id filter → we use Enrich `hiring`.
- Enrich with the full `people` group returns 6 MB → we request only `people.founders` and `people.cxos`.
- Freshworks' contact page is a 404 inside a 200 response → the callback uses Crustdata `funding.phone_number`.
- Our first scoring design blocked legitimate small vendors → we split it into risk vs verification confidence.
- `mcp` 2.x renamed FastMCP → pinned `mcp<2`.
- A quadratic regex on input like `1,1,1,…` (6 s) → bounded number patterns; a fuzz test now guards it.
- Claude-supplied `vendor_domain` could steer company resolution → it is now accepted only if it appears in the email.

More in `docs/BUILD_LOG.md`.

## The hardest case

A **real vendor's real mailbox** asks for payment to a new account (business email compromise via a compromised mailbox). Every identity check passes. PayeeGuard follows the money instead: Freshworks has been paid 14 times, never to this account, and the same account was used to impersonate Zoho minutes earlier. The verdict is `mule_campaign` + `compromised_vendor_mailbox`, so it's BLOCK, and the case file links both attempts into one campaign (samples 05 → 06).

## The testing: how we know it works

- `cli.py eval`: 10 samples with expected verdict **and** attack type (`samples/expected.json`), plus 3 onboarding requests, on a fresh seeded graph.
- `pytest`: every section-13 edge case has a named test. That covers extraction E1–E16, decisions S1–S11, gate P1–P5, runtime M1–M7 and privacy Y1–Y4, plus the scoring table 10.5 and the investigator patterns.
- A fuzz test: 400 mutated samples, checking that nothing crashes, nothing is BLOCKed without a contradiction, nothing is PAYed with a hard fact or bank change, and no full account number ever appears in a report.
- `cli.py ablate`: re-decides each sample with each check removed, showing which evidence decides it (without G3, the hero is a HOLD).
- Determinism: the same input gives an identical verdict, rule and report text.

## What comes next (and why not today)

| Area | Next step | Why not today |
|---|---|---|
| Email intake | Gmail / Microsoft Graph watcher; SPF/DKIM/DMARC results | needs customer mailbox access |
| ERP | Tally, Zoho Books, NetSuite, SAP vendor master + payment history; push holds onto bills | needs customer systems |
| Bank ownership | penny drop (IN), Confirmation of Payee (UK), Verification of Payee (EU) | needs partnerships |
| Calibration | fit per-family weights on reviewer outcomes; report precision / recall / false-hold rate | needs ~6 months of labelled data |
| Scale & security | Postgres, multi-tenant, SSO, roles, dual approval, secrets manager | 2–3 weeks of platform work |
| Compliance | DPDP / GDPR assessment for people data, retention, SOC 2 | needs legal review |

*Production-grade design, hackathon-scope integrations.*

---

## Repo map and owners

| Owner | Files |
|---|---|
| Yaksh (A) | `models.py`, `client.py`, `geo.py`, `checks_company.py`, `checks_channel.py`, `checks_behaviour.py` |
| Aditya (B) | `graph.py`, `checks_payment.py`, `checks_signatory.py`, `audit.py`, seed loader |
| Bahul (C) | `extract.py`, `scoring.py`, `investigator.py`, `actions.py`, `report.py`, `gate.py`, `pipeline.py`, `ablation.py`, `wiring.py`, `config.py`, `server.py`, `cli.py`, README |

**Integration points:** everything the pipeline needs from A and B goes through `payeeguard/wiring.py`.
- Checks are discovered as `check_*(inv, ctx, client, graph) -> list[Evidence]` in each `checks_*.py`, in source order.
- C1 should set `ctx["company_id"]` and `ctx["company"]` (the Enrich record), and C6 should set `ctx["callback"]` (`{"phone", "email", "source"}`).
- The graph interface (`add_edge`, `account_node`, `accounts_paid`, `cases_flagged`, `revoke_case_flags`) is documented at the top of `wiring.py`. An in-memory stand-in is used until `graph.py` lands.
- Mode 2 calls `checks_company.onboard_facts(vendor_name=, website=, client=, claimed_*=)` and expects `(list[Evidence], facts)`.
