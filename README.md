# PayeeGaurd

## Graph: the payment-trust memory

PayeeGuard keeps its own graph of who was paid where (`payeeguard/graph.py`, SQLite `data/graph.db`, WAL mode). Crustdata tells us *who* a vendor and sender are; the graph tells us *where the money has gone before*. Every edge carries a citation, so any path in a report is sourced.

| rel | src → dst | Written when |
|---|---|---|
| `OWNS_DOMAIN` | company → domain | every run (C1, Crustdata `basic_info.all_domains`) |
| `EMPLOYED_AT_CURRENT` / `EMPLOYED_AT_PAST` | person → company | C3 resolves the sender |
| `LEADS` | person → company | insider check (P1) |
| `SENT`, `CLAIMS_VENDOR`, `PAYS_TO` | email → invoice → company / account | every verify (idempotent) |
| `PAID`, `PAID_FROM_DOMAIN` | company → account / domain | **only** the payment gate or a human approval |
| `FLAGGED_IN` | account / email / lookalike domain → case | **only** on BLOCK |

**Accounts are never stored.** An account becomes `account:` + HMAC-SHA256(`PAYEEGUARD_FP_SECRET`, normalised IBAN or `SWIFT|IFSC:account`). Labels show only the last 4 digits and the country (`••5432 GB`). Three strengths of fingerprint:

- **strong** (IBAN, or bank code + full number): may drive a hard BLOCK (G3) or a hard duplicate (G4).
- **alias** (full number without a bank code): matches history and G2. It is never hard, because two banks can issue the same number. If both sides name *different* banks, it doesn't match at all.
- **weak** (bank code + last 4): only for G1 history comparison. It is never used for G2/G3.

**The checks that read it (`checks_payment.py`):**

| Check | Fires when | Weight |
|---|---|---|
| G1 account history | known vendor, account never paid before | 25 (verified = `money` confirmed) |
| G2 other vendor | the account was paid to a different vendor | 30 (allow-list for factoring) |
| G3 flagged | the account appears in a BLOCKED case | **hard** |
| G4 duplicate | invoice number already paid: same account 40, different account **hard**; no number: same amount ±1% in 14 days 15 | hard / 40 / 15 |
| C9 amount | more than 3× the median of ≥ 3 prior payments | 10 |
| G5 ambiguous | two different accounts in one email | 25 |

**The hero case (05 → 06).** Sample 05 (a Zoho lookalike) is blocked, and the gate writes `FLAGGED_IN` for its UK account. Sample 06 then arrives from Freshworks' *real* domain: every identity check passes. The graph shows that Freshworks was paid 14 times, never to this account (G1), and that this exact account was used in the Zoho case minutes earlier (G3, hard). The verdict is BLOCK, and the report shows the money trail with a citation on each edge.

**Rules that keep it honest:**

- Every query excludes the current case, so a case never matches itself (G-1).
- Verify writes only idempotent edges, so re-running gives the same verdict (G-2).
- Revoked flags are ignored after a human overrides a wrong BLOCK (G-7).
- A BLOCK caused by a compromised real mailbox does not flag the vendor's real domain.

**Seed:** `python cli.py seed` (or `python -m payeeguard.graph seed [--fresh]`) loads `samples/seed_history.json`: 14 Freshworks payments and 6 Zoho payments. It resolves each domain with Company Identify (free, cached) and is safe to run twice.

## Robustness

Missing data is never fraud. A check that can't run returns `unchecked` with a reason (`api_error`, `not_enabled`, `no_data`, `not_applicable`), which lowers confidence (→ HOLD) and never adds risk. Each behaviour below has a named offline test in `tests/`:

| Situation | Behaviour | Test |
|---|---|---|
| Re-running a blocked case | doesn't see its own flags | `test_query_excludes_own_case` |
| Same text verified twice | same evidence, no new edges | `test_verify_idempotent` |
| Paid case paid again | one payment recorded | `test_gate_no_double_pay` |
| Already-paid invoice re-sent (sample 09) | G4 = 40 → HOLD | `test_duplicate_new_text` |
| Last-4-only account | G1 only; never G2/G3 | `test_weak_fp_never_hard` |
| Same number, no bank code | never a hard block | `test_codeless_number_in_blocked_case_is_not_hard` |
| Shared account (factoring) | allow-list suppresses G2 | `test_shared_account_allowlist` |
| Wrong BLOCK overridden | flags revoked and ignored | `test_override_revokes_flags` |
| Approved bank change | both old and new accounts verify | `test_approved_bank_change` |
| Seed offline, empty cache | clear message on what to do | `test_seed_offline_message` |
| DB missing / locked | created on first use; waits on lock | `test_graph_autocreate`, `test_locked_database_waits_instead_of_failing` |
| `PAYEEGUARD_FP_SECRET` missing or short | graph refuses to start | `test_fp_secret_required` |
| IBAN spaces/lowercase, dashes, `…XXX` SWIFT | normalised before fingerprinting | `test_bank_normalisation` |
| Two accounts in one email; quoted history | G5 = 25; `>` lines ignored | `test_multiple_accounts`, `test_quoted_history_accounts_ignored` |
| "Pay to account on file" | ledger account used for a known vendor | `test_account_on_file` |
| Promotion (past + current at vendor) | current employee | `test_promotion_not_ex_employee` |
| Several current jobs | current; others listed | `test_multiple_current_jobs` |
| Two people with the same name | unchecked unless the title picks one | `test_same_name_people` |
| Person Identify 403 | straight to Person Search, no retry | `test_c3_identify_403_falls_back` |
| Crustdata down | C3 unchecked/api_error | `test_api_down_is_unchecked` |
| A check crashes | that row only becomes unchecked | `test_check_crash_isolated` |
| Audit log edited, reordered or deleted | first broken line reported | `test_audit_tamper_detected` |
| Raw text, full IBAN or key in outputs | never in the graph, evidence or audit log | `test_no_full_account_in_outputs`, `test_sanitise_never_writes_secrets` |

Run them offline: `python -m pytest tests/`.

**Known limits** (said plainly):

- Crustdata data can be stale, for example someone who left recently may still show as current. R3 still forces HOLD on any bank change, so stale data can't cause a PAY.
- Truncating the newest audit lines leaves a valid chain; the case file quotes the latest audit hash so it can be cross-checked.
- One account given in two formats (IBAN in one email, SWIFT + number in another) is treated as two accounts. That's the safe direction: HOLD.
