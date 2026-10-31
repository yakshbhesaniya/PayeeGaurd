# PayeeGuard

**The trust check every human or AI accounts-payable agent runs before money moves.**

PayeeGuard takes an invoice or bank-change email it has never seen and does the job an accounts-payable fraud
analyst is paid to do. It resolves the vendor and the sender in **Crustdata's identity graph**, follows the money
through **your own payment history**, names the attack if there is one, and **finishes the work**:

- **PAY:** a payment released through a gate.
- **HOLD:** a callback script and verification email to the vendor's *independently sourced* contact, and an
  exception ticket for a human.
- **BLOCK:** a complaint-ready fraud case file.

Every decision is deterministic and cites a source for every fact.

> *Every identity check passed. The money trail didn't.*

Built at **ContextCon 03, IIT Bombay (Agentic Services Hackathon), Oct 31 2026** by Yaksh, Aditya and Bahul.

---

## Contents

1. [The service](#1-the-service-who-pays-for-this-today)
2. [What PayeeGuard does](#2-what-payeeguard-does)
3. [How it decides](#3-how-it-decides)
4. [The hardest case: the hero demo](#4-the-hardest-case-the-hero-demo)
5. [Run it](#5-run-it)
6. [How we know it works](#6-how-we-know-it-works)
7. [The approaches we weighed](#7-the-approaches-we-weighed)
8. [What failed along the way](#8-what-failed-along-the-way)
9. [Privacy and guardrails](#9-privacy-and-guardrails)
10. [What comes next, and why not today](#10-what-comes-next-and-why-not-today)
11. [Project layout](#11-project-layout)

---

## 1. The service: who pays for this today

Accounts-payable teams, vendor-master teams, payment-fraud analysts and audit firms are paid to answer one
question before money leaves: **are we paying who we think we're paying?**

- Two in five businesses have been hit by invoice fraud, averaging **$367,000 per incident**; only a quarter have
  automated defences ([Ivalua, Sept 2026](https://www.ivalua.com/press-releases/two-in-five-businesses-fall-victim-to-invoice-fraud-with-losses-averaging-367000/)).
- Vendor impersonation is the most common tactic behind business email compromise
  ([AFP 2026 survey via Beancount](https://beancount.io/blog/2026/07/20/ai-fake-invoice-fraud-accounts-payable-guide)).
- AI agents are starting to pay invoices on their own, and nobody checks who they pay.

**Why it's new:** existing payee-verification products focus on bank-account ownership. PayeeGuard checks whether
the **request** is legitimate: who sent it, whether they really work there, whether this vendor has ever been paid
to this account, and whether the account appeared in another attack. It does this by combining a people-and-company
identity graph (Crustdata) with a money-trail graph built from your own payments.

**Business model:** a per-verification fee for AP teams and AI-payments platforms (embedded through MCP or HTTP),
plus a monthly fee for vendor-master monitoring. About 3–4 Crustdata credits per new vendor, ~0 for a cached one,
against a $367k average loss.

---

## 2. What PayeeGuard does

| Mode | Input | Output |
|---|---|---|
| **Invoice check** | Any invoice or bank-change email (pasted text, HTML or forwarded) | PAY / HOLD / BLOCK, the named attack, cited evidence, finished work |
| **Vendor onboarding (KYB)** | A new vendor's name and website | APPROVE / REVIEW / REJECT: is it real, who runs it, how old, is it trading, anything off |
| **Vendor-master screening** | A CSV of all your vendors | Cross-vendor alerts: shared bank accounts, duplicate vendor records, lookalike domains, accounts used in past attacks |
| **Payment gate** | A case id | Releases a simulated payment only if the case is PAY or a human approved it |
| **Agent guard API** | `POST /api/guard` from any AI payment agent | `{allow, decision, reasons, next_step}` before the agent pays |
| **Live inbox (automatic)** | A real Gmail mailbox (Inbox + Spam) watched every 10 s | Every new invoice email is verified with no clicks and appears in the desk's Live mailbox; HOLD/BLOCK posts a Slack alert |
| **Relationship history** | Our own Slack channels + mailbox (12 months) | If someone on our side ended the relationship ("we will not continue with X, don't send invoices") and nobody renewed, a new invoice from X is stopped and the message is cited |
| **Memory** | Persistent payment-trust graph (survives refreshes and restarts) | Vendors and the accounts they're paid to, accounts tied to blocked attacks, onboarded vendors, recent cases: every new invoice is checked against all of it |

**Interfaces:** a web desk (`web.py`), a CLI (`cli.py`), and an **MCP server** (`server.py`) so Claude Desktop or
Claude Code can act as the AP agent. Claude reads and orchestrates; PayeeGuard decides.

### The checks

| Family | Checks | Data source |
|---|---|---|
| **Company** | C1 vendor resolves to a real company · C4 shell signals · C12 India GSTIN/PAN validity and consistency | Crustdata Company Identify/Enrich; GSTIN checksum |
| **Channel** | C2 sender domain belongs to the vendor (safe lookalike rule incl. homoglyphs, IDN, brand-in-subdomain) · C2b free mail · C2c display-name spoofing · C8 domain age and MX · C10 Reply-To mismatch | Crustdata domains; RDAP; DNS-over-HTTPS |
| **Signatory** | C3 the sender's employment timeline: current employee, ex-employee, wrong person, role authority | Crustdata Person Search |
| **Payment** | G1 account vs this vendor's history · G2 account used by another vendor · G3 account used in a blocked attack · G4 duplicate invoice · G5 several accounts in one email · C5 bank country vs offices · C9 amount anomaly · C11 IBAN/SWIFT/IFSC validity | Payment-trust graph; Crustdata locations; checksums |
| **Relationship** | C15 our own Slack + email history: termination with no later renewal (attack *terminated_vendor*) | Slack Web API (bot token), IMAP (read-only) |
| **Behaviour** | C14 quoted pressure tactics: secrecy, channel switch, authority, threats, crypto/gift cards, urgency (incl. Hinglish) | Phrase lists in `policy.toml` |
| **Integrity** | X1 Claude's fields must match the email text · X2 conflicting totals | Grounding rule |

### Crustdata endpoints used

Company Identify (free) · Company Enrich (domains, offices, headcount, hiring, funding, official phone/email,
founders and CXOs) · Person Search (employment timeline with `crustdata_company_id`, `start_date`, `end_date`,
`function_category`) · Web Fetch (callback fallback) · Watchers (opt-in). The **Crustdata MCP server** can run next
to ours in Claude for follow-up research; it never changes a verdict.

---

## 3. How it decides

The decision is plain Python, so the same input always gets the same verdict and every point is traceable.

- **Risk** is the sum of each family's **strongest** contradicted signal, so one fact never counts twice.
  Behaviour sums within its family, capped at 25.
- **Confidence** is the count of three verified facts:
  - **WHO:** the vendor resolved, or is known in our history;
  - **CHANNEL:** the sender domain belongs to it;
  - **MONEY:** the account matches history, or for a first-time vendor the geography and bank format check out.
- **Missing data never adds risk.** It only lowers confidence, which leads to a HOLD, never a BLOCK.

| Rule | Condition | Decision |
|---|---|---|
| R1 | Any hard fact (lookalike domain, account used in a blocked attack, re-sent paid invoice to a new account) | **BLOCK** |
| R2 | Risk ≥ 60 | **BLOCK** |
| R3 | Bank change requested, new account on a known vendor, several accounts, missing/negative amount, or Crustdata unavailable | **HOLD** (never auto-paid) |
| R4 | Risk ≥ 25 | **HOLD** |
| R5 | Confidence HIGH | **PAY** |
| R6 | Confidence MEDIUM and amount ≤ auto-pay limit | **PAY** |
| R7 | Otherwise | **HOLD** (a human verifies) |

The **Fraud Pattern Investigator** then names the most likely attack, cites the facts behind it, and lists what it
**ruled out**. The attack types are: mule campaign, lookalike impersonation, compromised vendor mailbox, duplicate
replay, ex-employee, unauthorised change, unverified signatory, payment-method fraud and unverifiable vendor.

---

## 4. The hardest case: the hero demo

A real vendor's **real mailbox** asks you to pay a new account. Every identity check passes, so most tools wave it
through.

1. **Sample 05:** a lookalike Zoho domain asks for payment to a UK account. → **BLOCK** (lookalike), and
   PayeeGuard remembers that account.
2. **Sample 06:** a polite email from `billing@freshworks.com` (the real domain) asks to pay "our UK entity
   account". Crustdata confirms the real Freshworks, the domain, and the London office.
3. The **money trail** shows Freshworks has been paid 14 times, never to this account, and that this exact account
   was used in the Zoho attack minutes earlier. → **BLOCK**: *mule campaign + compromised vendor mailbox*.
4. **Finished work:** a complaint-ready case file linking both attempts, a do-not-pay notice, and a warning drafted
   to Freshworks' official contact. "Pay it anyway" → the **gate refuses**.

In the web desk, open **05** then **06** (or go to `http://127.0.0.1:8787/#06` after running 05).

---

### Scenario 2: "our own Slack says we ended this vendor" (fully automatic)

1. In Slack, someone posts: *"We will not continue with Freshworks after this quarter. Please don't send any more invoices."*
2. A perfectly normal Freshworks invoice is emailed to the watched Gmail inbox.
3. Within ~10 s, with no clicks, it appears in the desk's **Live mailbox**: **HOLD** (or **BLOCK** if sent from a
   personal Gmail), attack *"Invoice from a vendor we ended the relationship with"*, the Slack message quoted as
   evidence, and an alert in Slack.

*Every identity check passes (real company, real domain, the account we always pay), but our own conversations say
we ended this vendor, so the agent stops it.*

### Live integrations and safety rails

Each one switches on when its setting is in `.env`; without it the desk runs in demo mode with drafts. The masthead
shows a light per connection.

| Integration | Setting | Safety rail |
|---|---|---|
| Crustdata identity graph | `CRUSTDATA_API_KEY` | Responses cached; offline mode for the stage |
| Real inbox (Gmail IMAP) | `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | Inbox + Spam, read-only, nothing marked as read; only mail from the last 15 min before start onwards; non-payment mail never stored |
| Real email | `RESEND_API_KEY` (HTTPS) or `SMTP_*` | Goes to `SAFE_RECIPIENT` unless `EMAIL_TO_VENDOR=1`, so a demo never emails a real vendor |
| Slack alerts | `ALERT_WEBHOOK_URL` | One alert per HOLD/BLOCK case |
| Slack history (C15) | `SLACK_BOT_TOKEN` (`channels:read`, `channels:history`) | Reads only channels the bot is invited to; PayeeGuard's own alerts ignored |
| Payment gateway | Juspay sandbox / Razorpay test / Stripe test keys, or `PAYMENT_GATEWAY=simulate` | Test/sandbox only, live keys refused; the simulated gateway is explicit and labelled "no money moved" |

## 5. Run it

```bash
pip install httpx mcp python-dotenv idna pytest
cp .env.example .env     # add CRUSTDATA_API_KEY and PAYEEGUARD_FP_SECRET (generate: openssl rand -hex 32)
```

| Command | What it does |
|---|---|
| `python web.py` | Web desk on http://127.0.0.1:8787, keeping all memory (`--reset` wipes it). Deep links: `#06` hero, `#onb1` onboarding, `#vendors`, `#memory` |
| `python cli.py eval` | 10 invoice samples + 3 onboarding requests vs expected verdicts |
| `python -m pytest -q` | Full offline test suite (no network, no credits) |
| `python cli.py stress` | Metamorphic rewrites of every sample must keep the same verdict |
| `python cli.py ablate` | Which single check decides each verdict |
| `python cli.py verify samples/06_hero_freshworks_mule.txt` | One verification in the terminal |
| `python cli.py screen samples/vendor_master.csv` | Vendor-master screening |
| `python cli.py onboard "Freshworks" https://www.freshworks.com` | Vendor onboarding (KYB) |
| `python cli.py pay <case>` / `resolve <case> approve --by NAME --note TEXT` | Payment gate and human review |
| `python cli.py audit` | Verify the tamper-evident audit chain |
| `python server.py` | MCP server: tools `verify_payee`, `onboard_vendor`, `pay_invoice`, `resolve_case`, `case_status` |

**Live connections:** copy `.env.example` to `.env` and fill only what you want live (see the table above).

**Offline demo:** set `PAYEEGUARD_OFFLINE=1` to run purely from the response cache (survives venue Wi-Fi).

**Claude Desktop:** add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{ "mcpServers": { "payeeguard": { "command": "python", "args": ["D:\\Projects\\PayeeGaurd\\server.py"] } } }
```

---

## 6. How we know it works

| Layer | Result |
|---|---|
| Test suite (our tests + 221 scenarios ported from Aditya's and Bahul's branches) | **368 passed** |
| Live evaluation against Crustdata | **13/13** samples hit the expected verdict and attack type |
| Same evaluation fully offline | **13/13** |
| Metamorphic stress (CRLF, HTML, forwarded, quoted history, whitespace, signature noise) | **54/54** keep the same verdict |
| Fuzzing (300 mutated samples, 2,000 random strings, 150 garbled emails) | No crashes, no unsafe verdicts |
| Ablation | Removing the decisive check from any BLOCK still leaves a HOLD (defence in depth) |

The full edge-case matrix, the 27 bugs this process found and fixed, and the ablation table are in
[`docs/EDGE_CASE_REPORT.md`](docs/EDGE_CASE_REPORT.md).

---

## 7. The approaches we weighed

| Approach | Verdict |
|---|---|
| Let an LLM decide | **Rejected:** not reproducible, and a prompt-injected email could talk it into PAY |
| A checklist of independent rules adding up points (our first design) | **Rejected:** double-counted one fact, treated missing data as fraud, and missed attacks from a real vendor mailbox |
| Separate LLM agents per check | **Rejected:** cost and non-determinism with no gain in accuracy |
| **Identity graph (Crustdata) + money-trail graph + deterministic investigator** | **Chosen:** catches real-mailbox attacks, explains every verdict, fails safe |

**Where the LLM fits:** Claude reads messy emails and orchestrates through MCP. Any field it extracts is accepted
only if it appears in the email text, so it can point at facts but never invent them.

---

## 8. What failed along the way

- **Person Identify is 403 on our key:** signatory checks use Person Search on the employment timeline instead.
- **`/permissions` returns 404:** credits come from `/user/credits`.
- **Enrich with the full `people` group returns 6 MB:** we request `people.founders` / `people.cxos` only.
- **Freshworks' contact page is a 404 inside a 200:** the callback uses Crustdata's `funding.phone_number` first.
- **Crustdata returns HTTP 400 for invalid domains:** treated as "no match", then we fall back to the name.
- **`dns.google` is blocked on the venue network:** DNS-over-HTTPS falls back to Cloudflare.
- **Crustdata has no GST/tax-ID fields:** GSTIN is validated by checksum and compared with Crustdata's office
  states instead.
- **Our first scoring blocked legitimate small vendors and double-counted facts:** we split risk from confidence.
- **The venue Wi-Fi blocks Gmail's sending ports (465/587):** email goes out through Resend over HTTPS instead.
- **Gmail put a demo invoice in Spam:** the watcher now reads Inbox and Spam (scam invoices often land there).
- **Slack returned an empty history when a date filter was passed:** the 12-month window is applied in our own code.
- **Merging three teammates' parallel implementations** hit incompatible interfaces. We kept one integrated
  codebase and ported 221 of their test scenarios onto it, which exposed 23 real bugs (all fixed).

---

## 9. Privacy and guardrails

- Bank accounts are stored only as **keyed HMAC fingerprints**; outputs show the last 4 digits and country.
- **No raw email text** is saved; reports keep only extracted indicators.
- The API key and fingerprint secret live only in `.env` (git-ignored), and the graph refuses to start without a
  real secret.
- People data is used only to verify a claimed role.
- The inbox watcher is read-only, never marks mail as read, ignores older mail and drops anything that isn't a
  payment request; Slack and mailbox history are searched only for the vendor in question, and only the cited
  snippet is kept.
- Outgoing email goes to a safe recipient by default; the payment gateway accepts only test/sandbox credentials or
  an explicitly labelled simulation.
- **A human presses send.** PayeeGuard never emails anyone or moves money by itself: emails open pre-filled in your own mail app, the callback is a tap-to-call link to the official number, and payments are released to a queue for bank upload. Watchers are opt-in.
- Every decision and human action goes into a **hash-chained audit log**; tampering is reported at the exact line.
- The web server rejects path traversal, oversized bodies and non-object JSON, and never shows stack traces.

---

## 10. What comes next, and why not today

| Next step | Why not today |
|---|---|
| Bank-account ownership check (penny drop, Confirmation of Payee) | Needs bank or payment-provider partnerships |
| Live GST-portal / VAT (VIES) / LEI (GLEIF) verification | Needs licensed GSP access (GST); the others are a short integration |
| Mailbox and ERP connectors (Gmail/Outlook, Tally, Zoho Books, NetSuite, SAP) | Needs customer systems access |
| Calibrated weights from reviewer decisions | Needs months of labelled outcomes |
| Multi-tenant Postgres, SSO, roles, reviewer web inbox | Product hardening beyond a one-day build |
| DPDP Act / GDPR assessment, retention policy | Needs legal review |
| Watcher webhooks for continuous vendor monitoring | Needs a public endpoint |

The full roadmap is in [`docs/PayeeGuard_Master_Spec.md`](docs/PayeeGuard_Master_Spec.md), section 19a.

---

## 11. Project layout

```
payeeguard/        extract · client (Crustdata) · checks_* · graph · scoring · investigator · actions · report
                   gate · audit · pipeline · screening · ablation · geo · config
web.py, web/       web desk (stdlib server + HTML/CSS/JS)
cli.py             CLI          server.py   MCP server
samples/           10 invoice emails, onboarding requests, vendor master CSV, seed payment history (synthetic)
tests/             368 offline tests (edge cases, web, ported teammate scenarios)
docs/              Master spec, edge-case & verification report, build log
policy.toml        thresholds, FX, processors, free-mail list, tactic phrases
```

All sample data is synthetic with invented people; real company data comes live from Crustdata.
