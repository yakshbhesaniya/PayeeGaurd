# PayeeGuard

**The trust check every human or AI accounts-payable agent runs before money moves.**

PayeeGuard takes an invoice or bank-change email it has never seen, resolves the vendor and sender in
Crustdata's identity graph, follows the money through your own payment history, names the attack if there is
one, and finishes the job: a payment through a gate, a callback script and verification email for a human, or a
complaint-ready fraud case file. Every decision is deterministic and cites its sources.

> *Every identity check passed. The money trail didn't.*

## Run it

```bash
pip install httpx mcp python-dotenv idna pytest
cp .env.example .env          # add CRUSTDATA_API_KEY and a PAYEEGUARD_FP_SECRET (openssl rand -hex 32)

python web.py --reset         # web desk on http://127.0.0.1:8787  (#06 deep-links the hero case, #onb1 onboarding)
python cli.py eval            # 10 invoice samples + 3 onboarding requests vs expected verdicts
python -m pytest -q           # 77 offline edge-case tests (no network, no credits)
python cli.py verify samples/06_hero_freshworks_mule.txt
python server.py              # MCP server for Claude Desktop / Claude Code
```

Demo order for the hero: open **05** (Zoho lookalike → BLOCK, the account is remembered), then **06**
(a perfect email from Freshworks' real domain → BLOCK, because that account was just used in the Zoho attack).

## How it works

| Layer | What it does |
|---|---|
| Extract (`extract.py`) | Raw text → fields; forwarded-sender rule, lakh/euro amounts, IBAN/SWIFT/IFSC. The server re-derives critical fields itself, so a prompt-injected email can't steer them |
| Identity (Crustdata) | Company Identify + Enrich (domains, offices, headcount, official contact), Person Search (employment timeline: current vs past employer, role) |
| Money trail (`graph.py`) | SQLite graph: vendor → paid accounts, account → blocked cases, paid invoices. HMAC fingerprints only, never raw account numbers |
| Decide (`scoring.py`) | Risk = sum of per-family maxima (no double counting); confidence = WHO · CHANNEL · MONEY facts. Missing data never adds risk; it lowers confidence → HOLD, never BLOCK |
| Investigate (`investigator.py`) | Names the attack (mule campaign, lookalike impersonation, compromised mailbox, duplicate, ex-employee, …) and what it ruled out |
| Finish (`actions.py`, `gate.py`) | Payment instruction / callback + email + ticket / case file; payment gate; human approve-reject-override; hash-chained audit log |
| Mode 2 (`onboard_vendor`) | "Is this new vendor a real, trading business? Who runs it, how old, anything off?" → APPROVE / REVIEW / REJECT |

## What failed along the way

- Crustdata Person Identify is 403 on our key → signatory checks use Person Search on the employment timeline.
- `/permissions` returns 404 → credits come from `/user/credits`.
- Enrich with the full `people` group returns 6 MB → request `people.founders` / `people.cxos` only.
- Freshworks' contact page is a 404 inside a 200 → callback uses Crustdata's `funding.phone_number` first.
- Crustdata returns HTTP 400 for invalid domains (e.g. `.example`) → treated as "no match", fall back to the name.
- `dns.google` is blocked on the venue network → DNS-over-HTTPS falls back to Cloudflare.
- Our first scoring design blocked legitimate small vendors and double-counted one fact → risk vs confidence.

Full spec, edge-case matrix and roadmap: [`docs/PayeeGuard_Master_Spec.md`](docs/PayeeGuard_Master_Spec.md).
Nothing here sends email or moves money: every outbound artifact is labelled SIMULATED / DRAFT.
