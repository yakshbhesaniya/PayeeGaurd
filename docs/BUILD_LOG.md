# Build log: what failed and what we changed

One line per failure, newest last. Feeds the README "What failed" section (spec 19b).

## Found before building (spec 6.2)
- Person Identify returns 403 `permission_error` on our key → C3 goes straight to Person Search (re-confirmed live 2026-10-31).
- `/permissions` returns 404 → skipped.
- Enrich with the full `people` group returns 6 MB → request `people.founders` / `people.cxos` subfields only.
- Freshworks' contact page is a 404 inside a 200 response → use Crustdata `funding.phone_number` first.
- Job Search has no company-id filter → use Enrich `hiring`.
- The first scoring design blocked legitimate small vendors → risk vs confidence.

## Graph and people (Aditya)
- Code-less account numbers were first treated as strong fingerprints, so the same number at another bank could hard-block a legitimate payee → split into an `alias` fingerprint: matches history, never hard, and ignored when both sides name different banks.
- Blocking a compromised real mailbox (sample 06) would have flagged freshworks.com itself → `record_block` flags a domain only when the caller passes it (lookalikes).
- Weak (last-4) fingerprints could confirm an account that the full number contradicts → when the invoice has the full number, only full-number ids decide.
- Person Search returns `start_date` with a time (`2025-04-01T00:00:00`) → dates are sliced before display.
- Person Search word-match (`(.)`) can return near names → a candidate counts only if every token of the claimed name is in the profile name.
- Audit entries are written with sorted keys, so a naive tamper test edited nothing → test fixed to edit the real field.
