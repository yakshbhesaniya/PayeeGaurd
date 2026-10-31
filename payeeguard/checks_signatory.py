"""SIGNATORY family: C3 resolves the sender to a person and reads their employment timeline.

Not found is `unchecked` (Crustdata doesn't index every AR clerk). Only positive conflicts add risk:
ex-employee (35), works elsewhere (30), real employee outside a payments role asking for a bank change (20).
"""

import re

from . import config, geo
from .client import NotEnabled
from .evidence import describe, ev, first_match, reason_for, unchecked

FIN_FUNCTIONS = ("financ", "accounting", "accounts")


def _jobs(p, kind):
    return ((p.get("experience") or {}).get("employment_details") or {}).get(kind) or []


def _search(client, conditions):
    body = {"filters": {"op": "and", "conditions": conditions}, "limit": 5, "fields": ["basic_profile", "experience"]}
    return (client.post("/person/search", body) or {}).get("profiles") or []


def _is_finance(job: dict, title: str | None) -> bool:
    fc = (job.get("function_category") or "").lower()
    t = (title or job.get("title") or "").lower()
    kws = config.policy()["signatory"]["payments_title_keywords"]
    return any(f in fc for f in FIN_FUNCTIONS) or any(k in t for k in kws)


def _tokens(name: str | None) -> list[str]:
    import unicodedata
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return [t for t in re.findall(r"[a-z]+", n) if len(t) > 1]


def _same_name(claimed: str | None, profile: str | None) -> bool:
    """First and last name tokens must both match (order-insensitive); middle names may differ."""
    a, b = _tokens(claimed), _tokens(profile)
    if not a or not b:
        return False
    if len(a) == 1 or len(b) == 1:
        return a[0] in b or b[0] in a
    return {a[0], a[-1]} <= set(b) or {b[0], b[-1]} <= set(a)


def check_c3(inv, ctx):
    claim = "Sender works at the vendor"
    client, cid, company = ctx["client"], ctx.get("company_id"), ctx.get("company") or {}
    if not inv.sender_name:
        return [unchecked("C3_signatory", "SIGNATORY", claim, "Role mailbox or no personal name: not applicable",
                          "extract", "not_applicable")]
    if cid is None:
        return [unchecked("C3_signatory", "SIGNATORY", claim, "Vendor not resolved, cannot check employment",
                          "crustdata:person/search", "no_data")]
    info = company.get("basic_info") or {}
    vendor = info.get("name") or inv.vendor_name
    domain = geo.registrable_domain(info.get("primary_domain"))
    profiles, by_email, source = [], False, ""
    free = geo.email_domain(inv.sender_email) in config.policy()["channel"]["free_mail"]
    try:
        if inv.sender_email and not free:
            try:
                best, _ = first_match(client.post("/person/identify", {"emails": [inv.sender_email]}))
                if best:
                    profiles, by_email = [best["person_data"]], True
                    source = "crustdata:person/identify?emails=" + inv.sender_email
            except NotEnabled:
                pass  # 403 on our key (spec 6.2): go straight to Person Search
        name_cond = {"field": "basic_profile.name", "type": "(.)", "value": inv.sender_name}
        if not profiles and domain:
            profiles = _search(client, [{"field": "experience.employment_details.current.company_website_domain",
                                         "type": "in", "value": [domain]}, name_cond])
            source = f"crustdata:person/search?company_website_domain={domain}&name={inv.sender_name}"
        if not profiles and vendor:
            profiles = _search(client, [{"field": "experience.employment_details.company_name", "type": "in",
                                         "value": [vendor]}, name_cond])
            source = f"crustdata:person/search?company_name={vendor}&name={inv.sender_name}"
    except Exception as e:  # noqa: BLE001
        return [unchecked("C3_signatory", "SIGNATORY", claim, f"Person lookup failed: {describe(e)}",
                          "crustdata:person/search", reason_for(e))]

    if not by_email:
        # Search is a word match, so "Asha Rao" can return "Asha Menon": keep only profiles whose name really matches.
        profiles = [p for p in profiles if _same_name(inv.sender_name, (p.get("basic_profile") or {}).get("name"))]
    current = [p for p in profiles if any(j.get("crustdata_company_id") == cid for j in _jobs(p, "current"))]
    ex = [p for p in profiles if p not in current and any(j.get("crustdata_company_id") == cid for j in _jobs(p, "past"))]
    if current:
        claimed = (inv.sender_title or "").lower()
        pick = current[0] if len(current) == 1 else next(
            (p for p in current if claimed and claimed in (p.get("basic_profile") or {}).get("current_title", "").lower()),
            None)
        if pick is None:  # edge case D14
            return [unchecked("C3_signatory", "SIGNATORY", claim,
                              f"{len(current)} people named {inv.sender_name} at {vendor}; cannot tell which", source)]
        pid = pick.get("crustdata_person_id")
        ctx["person_id"] = pid
        job = next(j for j in _jobs(pick, "current") if j.get("crustdata_company_id") == cid)
        title = job.get("title")
        cite = f"crustdata:person/{pid}#experience.employment_details.current"
        others = [j.get("name") or "?" for j in _jobs(pick, "current") if j.get("crustdata_company_id") != cid]
        also = f" (also currently at {', '.join(others)})" if others else ""          # edge case D13
        out = [ev("C3_signatory", "SIGNATORY", claim, "verified",
                  f"{inv.sender_name} works at {vendor} as {title} since {(job.get('start_date') or '?')[:7]}{also}",
                  cite)]
        if ctx.get("account_change") and not _is_finance(job, title):
            out.append(ev("C3_role_authority", "SIGNATORY", "Sender's role covers payment details", "contradicted",
                          f"{inv.sender_name} is a real employee but works as '{title}', not in finance/payments, "
                          "and is asking to change bank details", cite, 20))
        return out
    if ex:
        p = ex[0]
        pid = p.get("crustdata_person_id")
        job = next(j for j in _jobs(p, "past") if j.get("crustdata_company_id") == cid)
        now_at = ", ".join(j.get("name") or "?" for j in _jobs(p, "current")) or "unknown"
        return [ev("C3_ex_employee", "SIGNATORY", "Sender currently works at the vendor", "contradicted",
                   f"{inv.sender_name} left {vendor} {(job.get('end_date') or '?')[:7]} ({job.get('title')}); now at {now_at}",
                   f"crustdata:person/{pid}#experience.employment_details.past", 35)]
    if by_email and profiles:
        p = profiles[0]
        now_at = ", ".join(j.get("name") or "?" for j in _jobs(p, "current")) or "unknown"
        return [ev("C3_works_elsewhere", "SIGNATORY", "Sender currently works at the vendor", "contradicted",
                   f"The sender's email belongs to someone working at {now_at}, not {vendor}",
                   f"crustdata:person/{p.get('crustdata_person_id')}#experience.employment_details.current", 30)]
    return [unchecked("C3_signatory", "SIGNATORY", claim,
                      f"No {inv.sender_name} among {vendor}'s current or past employees in Crustdata "
                      "(coverage gaps are common, so this adds no risk)", source)]
