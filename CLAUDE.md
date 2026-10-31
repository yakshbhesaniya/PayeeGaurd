# PayeeGuard — instructions for Claude Code

The full spec is the single source of truth. Read it before writing code:

@docs/PayeeGuard_Master_Spec.md

## Working rules

- You are helping ONE teammate: Yaksh (A), Aditya (B) or Bahul (C), see spec section 16. Edit only that person's files.
- `payeeguard/models.py` is frozen; never change it unless the user says the team agreed.
- Absence of data is never fraud: a check that cannot run returns `status="unchecked"` with an `unchecked_reason`, never `"contradicted"`.
- Decisions are deterministic Python (spec section 10). Never put an LLM call in the decision path.
- Every Evidence row and graph edge carries a citation in the spec section 5.1 format.
- Never log or persist the API key, full IBANs/account numbers or full raw emails.
- Nothing contacts real people or moves money; all outbound artifacts are labelled SIMULATED / DRAFT.
- Python 3.11, deps: httpx, mcp, python-dotenv, idna (dev: pytest, ruff). Keep code short, typed and readable.
- Work one file at a time and run it before moving on. Use `PAYEEGUARD_OFFLINE=1` once an endpoint is cached.
- Never commit `.env`.
