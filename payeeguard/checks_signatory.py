"""SIGNATORY family (spec section 7): is the sender who they claim?

C3_signatory resolves the sender to a Crustdata person and reads the employment timeline:
current employee (verified) · ex-employee (35) · matched by email but works elsewhere (30) ·
outside a payments role while asking for a bank change (20) · not found (unchecked, 0 risk).
Companies are matched by crustdata_company_id only, never by name.

C3b_insider (P1, off unless policy.buyer.buyer_domain is set): a first-time vendor led by
someone who currently works at our own company.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

from payeeguard.checks_payment import policy
from payeeguard.graph import Graph, case_id_for, email_domain, normalise_email, vendor_node
from payeeguard.models import Evidence, Invoice

log = logging.getLogger(__name__)

C3 = "C3_signatory"
C3_CLAIM = "Sender works at the vendor in a role that can request payment"
INSIDER = "C3b_insider"
INSIDER_CLAIM = "Vendor is not run by one of our own employees"
FIELD = "experience.employment_details"


def _ev(check: str, claim: str, status: str, detail: str, source: str, weight: int = 0,
        reason: Optional[str] = None, confirms: Optional[str] = None) -> Evidence:
    return Evidence(check=check, family="SIGNATORY", claim=claim, status=status,  # type: ignore[arg-type]
                    detail=detail, source=source, weight=weight if status == "contradicted" else 0,
                    unchecked_reason=reason if status == "unchecked" else None,
                    confirms=confirms if status == "verified" else None)  # type: ignore[arg-type]


def _reason(exc: Exception) -> str:
    """Yaksh's client raises NotEnabled on 403 (D2); everything else is an API error."""
    return "not_enabled" if type(exc).__name__ == "NotEnabled" else "api_error"


def _tokens(text: Optional[str]) -> list[str]:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return [t for t in re.split(r"[^a-z0-9]+", folded) if len(t) >= 2]


def _name_matches(claimed: Optional[str], profile_name: Optional[str]) -> bool:
    want, have = _tokens(claimed), set(_tokens(profile_name))
    return bool(want) and all(t in have for t in want)


def _title_matches(claimed: Optional[str], title: Optional[str]) -> bool:
    want, have = _tokens(claimed), set(_tokens(title))
    return bool(want) and all(t in have for t in want)


def _same_company(job: dict, company_id: int) -> bool:
    try:
        return int(job.get("crustdata_company_id")) == company_id
    except (TypeError, ValueError):
        return False


def _jobs(person: dict) -> tuple[list[dict], list[dict]]:
    emp = ((person.get("experience") or {}).get("employment_details") or {})
    cur = [j for j in emp.get("current") or [] if isinstance(j, dict)]
    past = [j for j in emp.get("past") or [] if isinstance(j, dict)]
    return cur, past


def _person_id(person: dict) -> Any:
    return person.get("crustdata_person_id")


def _person_name(person: dict) -> str:
    return (person.get("basic_profile") or {}).get("name") or "the sender"


def _profiles(resp: Any) -> tuple[list[dict], int]:
    """Person Search -> {"profiles": [...], "total_count": k}; tolerate a bare list."""
    if isinstance(resp, dict):
        profiles = [p for p in resp.get("profiles") or [] if isinstance(p, dict)]
        return profiles, int(resp.get("total_count") or len(profiles))
    if isinstance(resp, list):
        profiles = [p for p in resp if isinstance(p, dict)]
        return profiles, len(profiles)
    return [], 0


def _identified(resp: Any) -> list[dict]:
    """Person Identify -> [{"matches": [{"confidence_score", "person_data"}]}]; never index blindly (D6)."""
    out = []
    for group in resp if isinstance(resp, list) else []:
        for m in (group or {}).get("matches") or []:
            if isinstance(m, dict) and isinstance(m.get("person_data"), dict):
                out.append(m["person_data"])
    return out


def _search_body(conditions: list[dict], limit: int = 5) -> dict:
    return {"filters": {"op": "and", "conditions": conditions}, "limit": limit,
            "fields": ["basic_profile", "experience"]}


def _classify(person: dict, company_id: int) -> str:
    cur, past = _jobs(person)
    if any(_same_company(j, company_id) for j in cur):
        return "current"                     # D12: past + current at the vendor = promotion, still current
    if any(_same_company(j, company_id) for j in past):
        return "ex"
    return "elsewhere" if cur else "unrelated"


def _vendor_job(person: dict, company_id: int, current: bool) -> dict:
    cur, past = _jobs(person)
    jobs = [j for j in (cur if current else past) if _same_company(j, company_id)]
    key = "start_date" if current else "end_date"
    return max(jobs, key=lambda j: str(j.get(key) or ""))


def _has_payment_authority(job: dict, keywords: list[str]) -> bool:
    category = (job.get("function_category") or "").lower()
    if "financ" in category or "accounting" in category:
        return True
    title = (job.get("title") or "").lower()
    return any(re.search(rf"\b{re.escape(k.lower())}\b", title) for k in keywords if k)


def _is_free_mail(email: Optional[str], ctx: dict) -> bool:
    d = email_domain(email)
    free = {f.lower() for f in policy(ctx).get("channel", {}).get("free_mail", [])}
    return bool(d) and any(d == f or d.endswith("." + f) for f in free)


def _vendor_facts(inv: Invoice, ctx: dict) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """(crustdata_company_id, primary_domain, Crustdata name) of the resolved vendor."""
    try:
        cid = int(ctx["company_id"]) if ctx.get("company_id") not in (None, "") else None
    except (TypeError, ValueError):
        cid = None
    info = ((ctx.get("company") or {}).get("basic_info") or {}) if isinstance(ctx.get("company"), dict) else {}
    domain = info.get("primary_domain") or inv.vendor_domain
    return cid, (domain or "").lower().strip() or None, info.get("name") or inv.vendor_name


# ---------------------------------------------------------------- C3

def check_c3_signatory(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    cid, domain, vname = _vendor_facts(inv, ctx)
    vlabel = vname or "the vendor"
    if cid is None:
        return [_ev(C3, C3_CLAIM, "unchecked", "vendor not resolved in Crustdata, so the sender can't be matched to it",
                    "crustdata:company/identify -> no match", reason="no_data")]
    email, name = normalise_email(inv.sender_email), (inv.sender_name or "").strip()
    errors: list[str] = []
    calls_ok = 0
    via_email: list[dict] = []
    search_cite = f"crustdata:person/search?company_website_domain={domain}&name={name} -> total_count=0"

    # 1. Person Identify by email (403 on our key -> straight to search, no retry).
    if email and not _is_free_mail(email, ctx) and client is not None:
        try:
            via_email = _identified(client.post("/person/identify", {"emails": [email]}))
            calls_ok += 1
        except Exception as exc:
            errors.append(_reason(exc))
            log.info("C3 person/identify skipped: %s", type(exc).__name__)

    candidates: list[dict] = []
    if not via_email and name and client is not None:
        # 2. current employees of the vendor's domain with this name; 3. anyone who ever worked there.
        searches = []
        if domain:
            searches.append(([{"field": f"{FIELD}.current.company_website_domain", "type": "in", "value": [domain]},
                              {"field": "basic_profile.name", "type": "(.)", "value": name}],
                             f"crustdata:person/search?company_website_domain={domain}&name={name}"))
        if vname:
            searches.append(([{"field": f"{FIELD}.company_name", "type": "in", "value": [vname]},
                              {"field": "basic_profile.name", "type": "(.)", "value": name}],
                             f"crustdata:person/search?company_name={vname}&name={name}"))
        for conditions, cite in searches:
            try:
                profiles, total = _profiles(client.post("/person/search", _search_body(conditions)))
                calls_ok += 1
            except Exception as exc:
                errors.append(_reason(exc))
                log.info("C3 person/search failed: %s", type(exc).__name__)
                continue
            search_cite = f"{cite} -> total_count={total}"
            candidates = [p for p in profiles if _name_matches(name, _person_name(p))]
            if any(_classify(p, cid) in ("current", "ex") for p in candidates):
                break

    if via_email:
        candidates = via_email
    seen: dict[Any, dict] = {}
    for p in candidates:
        seen.setdefault(_person_id(p) or id(p), p)
    people = list(seen.values())
    relevant = [p for p in people if _classify(p, cid) in ("current", "ex")]

    if not relevant:
        if via_email and any(_classify(p, cid) == "elsewhere" for p in people):
            p = next(p for p in people if _classify(p, cid) == "elsewhere")
            jobs = ", ".join(j.get("name") or "?" for j in _jobs(p)[0])
            return [_ev(C3, C3_CLAIM, "contradicted",
                        f"{email} belongs to {_person_name(p)}, who works at {jobs}, not {vlabel}",
                        f"crustdata:person/{_person_id(p)}#{FIELD}.current", weight=30)]
        if calls_ok == 0 and errors:
            reason = "not_enabled" if all(e == "not_enabled" for e in errors) else "api_error"
            return [_ev(C3, C3_CLAIM, "unchecked", "Crustdata person lookup unavailable",
                        "crustdata:person/search -> error", reason=reason)]
        if not name and not via_email:
            return [_ev(C3, C3_CLAIM, "unchecked", "no sender name to look up", "extract:regex#sender_name",
                        reason="no_data")]
        return [_ev(C3, C3_CLAIM, "unchecked", f"{name or email} not found among {vlabel} people in Crustdata "
                    "(no penalty: absence is not fraud)", search_cite, reason="no_data")]

    if len(relevant) > 1:                                        # D14
        by_title = [p for p in relevant if _title_matches(
            inv.sender_title, _vendor_job(p, cid, _classify(p, cid) == "current").get("title"))]
        if len(by_title) != 1:
            return [_ev(C3, C3_CLAIM, "unchecked", f"{len(relevant)} people named {name} at {vlabel}; "
                        "can't tell which one sent this", search_cite, reason="no_data")]
        relevant = by_title

    person = relevant[0]
    pid, pname = _person_id(person), _person_name(person)
    ctx["person"], ctx["person_id"] = person, pid
    current = _classify(person, cid) == "current"
    job = _vendor_job(person, cid, current)
    if graph is not None:
        case = ctx.get("case_id") or case_id_for(inv.raw_text)
        try:
            graph.record_employment(case, pid, cid, current, pname)
        except Exception as exc:
            log.error("C3 graph write failed: %s", type(exc).__name__)

    if not current:
        left = str(job.get("end_date") or "")[:7] or "date unknown"
        return [_ev(C3, C3_CLAIM, "contradicted",
                    f"ex-employee: {pname} left {vlabel} {left} (was {job.get('title') or 'unknown role'})",
                    f"crustdata:person/{pid}#{FIELD}.past", weight=35)]

    title, since = job.get("title") or "unknown role", str(job.get("start_date") or "")[:10] or "unknown"
    others = [j.get("name") or "?" for j in _jobs(person)[0] if not _same_company(j, cid)]
    also = f"; also currently at {', '.join(others)}" if others else ""      # D13
    cite = f"crustdata:person/{pid}#{FIELD}.current"
    keywords = policy(ctx).get("signatory", {}).get("payments_title_keywords", [])
    if inv.bank_change_requested and not _has_payment_authority(job, keywords):
        return [_ev(C3, C3_CLAIM, "contradicted",
                    f"role authority: {pname} is {title} at {vlabel} "
                    f"({job.get('function_category') or 'no finance function'}), not a payments role, "
                    f"yet asks for a bank change{also}", cite, weight=20)]
    c1_verified = ctx.get("c1_verified", True)
    return [_ev(C3, C3_CLAIM, "verified", f"{pname} is a current {title} at {vlabel} since {since}{also}", cite,
                confirms=None if c1_verified else "who")]


# ---------------------------------------------------------------- C3b insider (P1)

def _leaders(company: Any) -> list[dict]:
    if isinstance(company, list):          # raw Enrich response
        for group in company:
            for m in (group or {}).get("matches") or []:
                if isinstance(m, dict) and isinstance(m.get("company_data"), dict):
                    return _leaders(m["company_data"])
        return []
    people = (company or {}).get("people") or {}
    out = []
    for group in ("founders", "cxos"):
        out += [p for p in people.get(group) or [] if isinstance(p, dict) and p.get("crustdata_person_id")]
    return out


def check_c3b_insider(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    buyer = (policy(ctx).get("buyer", {}).get("buyer_domain") or "").strip().lower()
    cid, _, vname = _vendor_facts(inv, ctx)
    if not buyer:
        return []                                                  # feature disabled: no row at all
    if cid is None or client is None:
        return [_ev(INSIDER, INSIDER_CLAIM, "unchecked", "vendor not resolved",
                    "crustdata:company/identify -> no match", reason="no_data")]
    case = ctx.get("case_id") or case_id_for(inv.raw_text)
    if graph is not None and graph.vendor_known(vendor_node(cid), case):
        return [_ev(INSIDER, INSIDER_CLAIM, "unchecked", "known vendor: insider check runs for first-time vendors only",
                    "graph:PAID", reason="not_applicable")]
    try:
        company = ctx.get("company") if _leaders(ctx.get("company")) else client.post(
            "/company/enrich", {"crustdata_company_ids": [cid], "fields": ["people.founders", "people.cxos"]})
        leaders = _leaders(company)
        if not leaders:
            return [_ev(INSIDER, INSIDER_CLAIM, "unchecked", "no founders or CXOs listed for the vendor",
                        f"crustdata:company/{cid}#people", reason="no_data")]
        ids = [p["crustdata_person_id"] for p in leaders]
        profiles, _ = _profiles(client.post("/person/search", _search_body(
            [{"field": "crustdata_person_id", "type": "in", "value": ids},
             {"field": f"{FIELD}.current.company_website_domain", "type": "in", "value": [buyer]}], limit=25)))
    except Exception as exc:
        return [_ev(INSIDER, INSIDER_CLAIM, "unchecked", "Crustdata lookup failed", "crustdata:person/search -> error",
                    reason=_reason(exc))]
    if graph is not None:
        for p in leaders:
            try:
                graph.record_leads(case, p["crustdata_person_id"], cid, (p.get("basic_profile") or {}).get("name", ""))
            except Exception as exc:
                log.error("insider graph write failed: %s", type(exc).__name__)
    insiders = [p for p in profiles if p.get("crustdata_person_id") in ids]
    if insiders:
        p = insiders[0]
        return [_ev(INSIDER, INSIDER_CLAIM, "contradicted",
                    f"{_person_name(p)} leads {vname or 'the vendor'} and currently works at {buyer}",
                    f"crustdata:person/{_person_id(p)}#{FIELD}.current", weight=35)]
    return [_ev(INSIDER, INSIDER_CLAIM, "verified", f"none of {len(leaders)} vendor leaders currently works at {buyer}",
                f"crustdata:company/{cid}#people")]


def check_signatory(inv: Invoice, ctx: dict, client: Any, graph: Optional[Graph]) -> list[Evidence]:
    """C3 + insider (P1); a crash in either becomes unchecked/api_error (S10)."""
    out: list[Evidence] = []
    for check, claim, fn in ((C3, C3_CLAIM, check_c3_signatory), (INSIDER, INSIDER_CLAIM, check_c3b_insider)):
        try:
            out += fn(inv, ctx, client, graph)
        except Exception as exc:
            log.error("%s crashed: %s", check, type(exc).__name__)
            out.append(_ev(check, claim, "unchecked", f"check failed ({type(exc).__name__})",
                           "payeeguard:check_error", reason="api_error"))
    return out
