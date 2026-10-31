"""COMPANY family + context checks: C1 vendor exists, C4 shell signals, C5 geography, C6 callback,
C7 watcher, and Mode 2 onboarding facts (spec 3.1, 7)."""

import logging
import re
from datetime import date, datetime

from . import geo
from .client import BadRequest
from .evidence import describe, ev, first_match, reason_for, unchecked

log = logging.getLogger(__name__)

ENRICH_FIELDS = ["basic_info", "headcount", "locations", "web_traffic", "funding", "hiring"]
MIN_CONFIDENCE = 0.8


def _cite(cid, path):
    return f"crustdata:company/{cid}#{path}"


def identify(client, *, domain=None, name=None):
    body = {"domains": [domain], "exact_match": True} if domain else {"names": [name]}
    return client.post("/company/identify", body)


def enrich(client, cid, fields=None):
    resp = client.post("/company/enrich", {"crustdata_company_ids": [cid], "fields": fields or ENRICH_FIELDS})
    best, _ = first_match(resp)
    return (best or {}).get("company_data")


def check_c1(inv, ctx):
    """Resolve the vendor to a crustdata_company_id. No match is `unchecked`, never `contradicted`."""
    client = ctx["client"]
    claim = "Vendor resolves to a real company in Crustdata"
    if not inv.vendor_domain and not inv.vendor_name:
        return [unchecked("C1_vendor", "COMPANY", claim, "No vendor name or domain in the request", "extract")]
    try:
        best, matches, how = None, [], ""
        if inv.vendor_domain:
            how = f"domains={inv.vendor_domain}"
            try:
                best, matches = first_match(identify(client, domain=geo.registrable_domain(inv.vendor_domain)))
            except BadRequest:
                best = None   # Crustdata rejects invalid domains with 400: treat as no match, try the name
        if best is None and inv.vendor_name:
            best, matches = first_match(identify(client, name=inv.vendor_name))
            how = f"names={inv.vendor_name}"
    except Exception as e:  # noqa: BLE001 - every failure becomes unchecked
        return [unchecked("C1_vendor", "COMPANY", claim, f"Company Identify failed: {describe(e)}",
                          "crustdata:company/identify", reason_for(e))]
    if best is None:
        return [unchecked("C1_vendor", "COMPANY", claim, f"No company found for {how}",
                          f"crustdata:company/identify?{how} -> no match")]

    sender_label = geo.label_of(geo.email_domain(inv.sender_email) or "x.x")
    if len(matches) > 1 and (matches[0]["confidence_score"] - matches[1]["confidence_score"]) < 0.05:
        overlap = [m for m in matches[:2] if sender_label and sender_label in
                   (m["company_data"]["basic_info"].get("primary_domain") or "")]
        if len(overlap) == 1:
            best = overlap[0]
        else:
            names = " / ".join(m["company_data"]["basic_info"]["name"] for m in matches[:2])
            return [unchecked("C1_vendor", "COMPANY", claim, f"Ambiguous: {names} score within 0.05",
                              f"crustdata:company/identify?{how}")]
    data, conf = best["company_data"], best.get("confidence_score") or 0
    cid = data["crustdata_company_id"]
    info = data.get("basic_info") or {}
    if conf < MIN_CONFIDENCE:
        return [unchecked("C1_vendor", "COMPANY", claim,
                          f"Best match {info.get('name')} has low confidence {conf:.2f}", _cite(cid, "basic_info"))]
    ctx["company_id"] = cid
    ctx["company"] = data
    try:
        full = enrich(client, cid)
        if full:
            ctx["company"] = full
    except Exception as e:  # noqa: BLE001
        log.warning("enrich failed for %s: %s", cid, describe(e))
        ctx["enrich_error"] = reason_for(e)
    domains = ", ".join(info.get("all_domains") or [info.get("primary_domain") or "?"])
    return [ev("C1_vendor", "COMPANY", claim, "verified",
               f"Resolved to {info.get('name')} (crustdata_company_id {cid}, confidence {conf:.2f}; domains {domains})",
               _cite(cid, "basic_info"), confirms="who")]


def company_domains(company: dict | None) -> set[str]:
    if not company:
        return set()
    info = company.get("basic_info") or {}
    doms = {geo.registrable_domain(d) for d in (info.get("all_domains") or [])}
    doms.add(geo.registrable_domain(info.get("primary_domain")))
    return {d for d in doms if d}


def company_countries(company: dict | None) -> set[str]:
    loc = (company or {}).get("locations") or {}
    out = {geo.office_country(a) for a in (loc.get("all_office_addresses") or [])}
    out.add(geo.to_iso2(loc.get("country")))
    hq = loc.get("headquarters") or ""
    if hq:
        out.add(geo.to_iso2(hq.rsplit(",", 1)[-1]))
    return {c for c in out if c}


def check_c4(inv, ctx):
    company, cid = ctx.get("company"), ctx.get("company_id")
    claim = "Vendor shows no shell-company signals"
    if not company or ctx.get("enrich_error"):
        return [unchecked("C4_shell", "COMPANY", claim, "No enriched company record", "crustdata:company/enrich",
                          ctx.get("enrich_error", "no_data"))]
    info = company.get("basic_info") or {}
    head = (company.get("headcount") or {}).get("total")
    wt = company.get("web_traffic") or {}
    signals = []
    year = info.get("year_founded")
    if year and year >= date.today().year - 1:
        signals.append(f"founded {year}")
    if isinstance(head, (int, float)) and head <= 3:
        signals.append(f"headcount {head}")
    if not wt or not any(wt.values()):
        signals.append("no web traffic data")
    if len(signals) >= 2:
        return [ev("C4_shell", "COMPANY", claim, "contradicted", "Shell signals: " + ", ".join(signals),
                   _cite(cid, "basic_info.year_founded,headcount.total,web_traffic"), 15)]
    detail = f"Established company: founded {year or '?'}, {head if head is not None else '?'} staff"
    if signals:
        detail += f" (single weak signal: {signals[0]})"
    return [ev("C4_shell", "COMPANY", claim, "verified", detail, _cite(cid, "basic_info.year_founded,headcount.total"))]


def check_c5(inv, ctx):
    company, cid = ctx.get("company"), ctx.get("company_id")
    claim = "Bank country is a country the vendor operates in"
    if not inv.remit_country:
        return [unchecked("C5_geo", "PAYMENT", claim, "No remit country in the request", "extract", "not_applicable")]
    countries = company_countries(company)
    if not countries:
        return [unchecked("C5_geo", "PAYMENT", claim, "Vendor locations unknown", "crustdata:company/enrich",
                          ctx.get("enrich_error", "no_data"))]
    listed = ", ".join(sorted(countries))
    if inv.remit_country in countries:
        return [ev("C5_geo", "PAYMENT", claim, "verified", f"Remit country {inv.remit_country}; vendor operates in {listed}",
                   _cite(cid, "locations.all_office_addresses"),
                   confirms=None if ctx.get("vendor_known") else "money")]
    return [ev("C5_geo", "PAYMENT", claim, "contradicted",
               f"Remit country {inv.remit_country}; vendor operates only in {listed}",
               _cite(cid, "locations.all_office_addresses"), 20)]


PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,18}\d")
CONTACT_PATHS = ["", "/contact", "/contact-us", "/company/contact", "/about/contact"]


def check_c6(inv, ctx):
    """Independent callback contact: Crustdata's own record first, then the official website. Never the invoice."""
    company, cid = ctx.get("company"), ctx.get("company_id")
    claim = "Independent contact found for callback"
    if not company:
        return [unchecked("C6_callback", "CONTEXT", claim, "Vendor not resolved", "crustdata", "no_data")]
    funding = company.get("funding") or {}
    phone, email = funding.get("phone_number"), funding.get("contact_email")
    website = (company.get("basic_info") or {}).get("website")
    if phone or email:
        ctx["callback"] = {"phone": phone, "email": email, "website": website,
                           "source": _cite(cid, "funding.phone_number")}
        return [ev("C6_callback", "CONTEXT", claim, "verified",
                   f"Official contact from Crustdata: {phone or '-'} / {email or '-'}", _cite(cid, "funding.phone_number"))]
    if not website:
        return [unchecked("C6_callback", "CONTEXT", claim, "No official website on record", _cite(cid, "basic_info.website"))]
    base = website.rstrip("/")
    try:
        pages = ctx["client"].post("/web/enrich/live", {"urls": [base + p for p in CONTACT_PATHS]})
    except Exception as e:  # noqa: BLE001
        ctx["callback"] = {"phone": None, "email": None, "website": website, "source": _cite(cid, "basic_info.website")}
        return [unchecked("C6_callback", "CONTEXT", claim, f"Web Fetch failed: {describe(e)}", base, reason_for(e))]
    for page in pages or []:
        if page.get("success") and page.get("http_status") == 200:  # edge case D16: 404 inside a 200
            text = re.sub(r"<[^>]+>", " ", page.get("content") or "")
            m = PHONE_RE.search(text)
            mail = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
            if m or mail:
                ctx["callback"] = {"phone": m.group(0).strip() if m else None, "email": mail.group(0) if mail else None,
                                   "website": page["url"], "source": page["url"]}
                return [ev("C6_callback", "CONTEXT", claim, "verified", f"Contact found on {page['url']}", page["url"])]
    ctx["callback"] = {"phone": None, "email": None, "website": website, "source": _cite(cid, "basic_info.website")}
    return [ev("C6_callback", "CONTEXT", claim, "verified", f"Use the official website {website}",
               _cite(cid, "basic_info.website"))]


def create_watch(client, cid) -> str:
    """C7 (on PAY): watch the vendor for domain/name changes. Returns a short status line."""
    body = {"entities": {"crustdata_company_ids": [cid]},
            "track": {"op": "or", "conditions": [{"field": "basic_info.primary_domain", "type": "changed"},
                                                 {"field": "basic_info.name", "type": "changed"}]},
            "fields": ["basic_info"], "config": {"trigger": {"type": "interval", "every_hours": 24}},
            "notifications": [{"type": "email", "to": "ap-alerts@example.com"}]}
    try:
        w = client.post("/watch/company", body)
        return f"Crustdata watcher {w.get('id')} monitors vendor {cid} for domain/name changes"
    except Exception as e:  # noqa: BLE001
        return f"Monitoring not enabled ({describe(e)})"


# ---- Mode 2: vendor onboarding (spec 3.1) ---------------------------------------------------------

def _insider(client, f, people, vendor_cid):
    """Insider fake-vendor check (P1): does a founder/executive of the new vendor work or have worked at OUR company?
    Uses employment history already returned by Enrich; one free Identify call to resolve our own company."""
    from . import config
    buyer = config.policy()["buyer"].get("buyer_domain")
    if not buyer:
        return
    try:
        best, _ = first_match(identify(client, domain=geo.registrable_domain(buyer)))
    except Exception as e:  # noqa: BLE001
        f["errors"].append(f"insider check: {describe(e)}")
        return
    if not best:
        return
    buyer_cid = best["company_data"]["crustdata_company_id"]
    if buyer_cid == vendor_cid:
        return
    for p in (people.get("founders") or []) + (people.get("cxos") or []):
        emp = (p.get("experience") or {}).get("employment_details") or {}
        for kind in ("current", "past"):
            for j in emp.get(kind) or []:
                if j.get("crustdata_company_id") == buyer_cid:
                    name = (p.get("basic_profile") or {}).get("name")
                    when = "currently works" if kind == "current" else f"worked until {(j.get('end_date') or '?')[:7]}"
                    f["off"].append(f"Conflict of interest: {name} (leader of this vendor) {when} at your company as "
                                    f"{j.get('title') or 'staff'} [crustdata:person/{p.get('crustdata_person_id')}]")
                    break


def onboard_facts(client, vendor_name, website, claims=None):
    """Collect cited facts answering: is it real, who runs it, how old, is it trading, anything off?"""
    claims = claims or {}
    host = re.sub(r"^https?://", "", website or "").split("/")[0]
    domain = geo.registrable_domain(host)
    f = {"domain": domain, "rows": [], "off": [], "leaders": [], "company": None, "cid": None, "errors": []}

    def row(q, a, src):
        f["rows"].append((q, a, src))

    best, by_name, name_matches = None, None, []
    try:
        if domain:
            try:
                best, _ = first_match(identify(client, domain=domain))
            except BadRequest:
                best = None
        if vendor_name:
            by_name, name_matches = first_match(identify(client, name=vendor_name))
    except Exception as e:  # noqa: BLE001
        f["errors"].append(describe(e))
    # Prefer a name match that has a domain on record (e.g. "Zoho Corporation" -> Zoho, not a domainless subsidiary).
    with_domain = [m for m in name_matches if (m["company_data"].get("basic_info") or {}).get("primary_domain")]
    by_name = with_domain[0] if with_domain else by_name
    named = (by_name or {}).get("company_data") or {}
    f["brand"] = (re.findall(r"[a-z0-9]+", (vendor_name or "").lower()) or [None])[0]
    if best is None and by_name and (by_name.get("confidence_score") or 0) >= MIN_CONFIDENCE:
        f["name_match"] = named
    if best is None:
        row("Is it real?", f"No company in Crustdata uses {domain}", f"crustdata:company/identify?domains={domain} -> no match")
        if named:
            real = geo.registrable_domain((named.get("basic_info") or {}).get("primary_domain"))
            f["name_company"] = named
            row("Name match", f"'{vendor_name}' resolves to {named['basic_info']['name']} whose domain is {real}",
                _cite(named["crustdata_company_id"], "basic_info.primary_domain"))
            f["real_domain"] = real
        return f
    data = best["company_data"]
    cid = data["crustdata_company_id"]
    f["cid"] = cid
    try:
        full = enrich(client, cid, ENRICH_FIELDS + ["people.founders", "people.cxos"]) or data
    except Exception as e:  # noqa: BLE001
        f["errors"].append(describe(e))
        full = data
    f["company"] = full
    info = full.get("basic_info") or {}
    row("Is it real?", f"Yes: {info.get('name')} (crustdata_company_id {cid}), {info.get('company_type') or 'type n/a'}",
        _cite(cid, "basic_info"))
    if named and named.get("crustdata_company_id") not in (None, cid):
        f["off"].append(f"Name '{vendor_name}' resolves to a different company ({named['basic_info']['name']})")
    people = full.get("people") or {}
    seen = set()
    for p in (people.get("founders") or []) + (people.get("cxos") or []):
        pid = p.get("crustdata_person_id")
        if pid in seen:
            continue
        seen.add(pid)
        bp = p.get("basic_profile") or {}
        cur = [j for j in ((p.get("experience") or {}).get("employment_details") or {}).get("current") or []
               if j.get("crustdata_company_id") == cid]
        since = (cur[0].get("start_date") or "")[:7] if cur else ""
        f["leaders"].append((bp.get("name"), (cur[0].get("title") if cur else bp.get("current_title")), since, pid))
    _insider(client, f, people, cid)
    lead = "; ".join(f"{n} ({t}{', since ' + s if s else ''})" for n, t, s, _ in f["leaders"][:5]) or "none listed"
    row("Who runs it?", lead, _cite(cid, "people.founders,people.cxos"))
    if not f["leaders"]:
        f["off"].append("No founders or executives on record")
    year = info.get("year_founded")
    row("How long has it existed?", f"Founded {year or 'unknown'}", _cite(cid, "basic_info.year_founded"))
    head = full.get("headcount") or {}
    hire = full.get("hiring") or {}
    yoy = (head.get("growth_percent") or {}).get("yoy")
    yoy = round(yoy, 1) if isinstance(yoy, (int, float)) else yoy
    row("Is it trading?", f"{head.get('total', '?')} staff (yoy {yoy if yoy is not None else 'n/a'}%), "
        f"{hire.get('openings_count', 0)} open roles, {(full.get('funding') or {}).get('num_funding_rounds', 0)} funding rounds",
        _cite(cid, "headcount.total,hiring.openings_count"))
    if isinstance(yoy, (int, float)) and yoy < -30:
        f["off"].append(f"Headcount fell {yoy}% in a year")
    if domain not in company_domains(full):
        f["off"].append(f"Website {domain} is not among the company's domains on record")
    if year and year >= datetime.now().year - 1:
        f["off"].append(f"Very young company (founded {year})")
    total = head.get("total")
    if claims.get("claimed_headcount") and isinstance(total, (int, float)):
        c = claims["claimed_headcount"]
        if abs(total - c) > 0.25 * c:
            f["off"].append(f"Claims {c} staff; Crustdata shows {total}")
    countries = company_countries(full)
    cc = geo.to_iso2(claims.get("claimed_country"))
    if cc and countries and cc not in countries:
        f["off"].append(f"Claims to operate in {cc}; offices on record: {', '.join(sorted(countries))}")
    if claims.get("claimed_offices"):
        n = len((full.get("locations") or {}).get("all_office_addresses") or [])
        if n and abs(n - claims["claimed_offices"]) >= 2:
            f["off"].append(f"Claims {claims['claimed_offices']} offices; {n} on record")
    funding = full.get("funding") or {}
    f["contact"] = {"phone": funding.get("phone_number"), "email": funding.get("contact_email"),
                    "website": info.get("website")}
    return f


def check_c12_tax(inv, ctx):
    """India GST/PAN: offline GSTIN checksum, PAN entity type vs company type, GSTIN state vs the vendor's Indian
    offices from Crustdata. (Crustdata has no tax-ID fields; live GST-portal verification needs a licensed GSP API.)"""
    meta = ctx.get("meta")
    gstin, pan = (meta.gstin, meta.pan) if meta else (None, None)
    claim = "Tax registration (GSTIN/PAN) is valid and consistent with the vendor"
    if not gstin and not pan:
        return [unchecked("C12_tax_id", "COMPANY", claim, "No GSTIN or PAN in the request", "extract", "not_applicable")]
    src = "extract:regex#gstin (GST checksum, mod-36)"
    if gstin and not geo.gstin_valid(gstin):
        return [ev("C12_tax_id", "COMPANY", claim, "contradicted",
                   f"GSTIN {gstin} fails its checksum or state code: mistyped or fabricated", src, 20)]
    out = []
    emb_pan = gstin[2:12] if gstin else None
    if pan and emb_pan and pan != emb_pan:
        out.append(ev("C12_tax_id", "COMPANY", claim, "contradicted",
                      f"PAN {pan} does not match the PAN inside GSTIN {gstin} ({emb_pan})", src, 15))
    the_pan = emb_pan or pan
    entity = geo.PAN_ENTITY.get(the_pan[3]) if the_pan and len(the_pan) == 10 else None
    company = ctx.get("company") or {}
    ctype = ((company.get("basic_info") or {}).get("company_type") or "").lower()
    if entity == "individual" and ("company" in ctype or "public" in ctype or "private" in ctype):
        out.append(ev("C12_tax_id", "COMPANY", claim, "contradicted",
                      f"{company['basic_info'].get('name')} is a {ctype} but the PAN belongs to an individual "
                      f"({the_pan[3]} = individual): classic shell / personal-account pattern",
                      f"{src} vs crustdata:company/{ctx.get('company_id')}#basic_info.company_type", 15))
    if gstin and company:
        states = {geo.office_state_in(a) for a in ((company.get("locations") or {}).get("all_office_addresses") or [])}
        states.discard(None)
        st = geo.GST_STATES.get(gstin[:2])
        if states and st not in states:
            out.append(ev("C12_tax_id", "COMPANY", claim, "contradicted",
                          f"GSTIN is registered in {st}, but the vendor's Indian offices are in {', '.join(sorted(states))}",
                          f"{src} vs crustdata:company/{ctx.get('company_id')}#locations.all_office_addresses", 10))
        elif states:
            out.append(ev("C12_tax_id", "COMPANY", claim, "verified",
                          f"GSTIN valid ({entity or 'entity'} PAN), registered in {st} where the vendor has an office",
                          f"{src} + crustdata:company/{ctx.get('company_id')}#locations"))
    if out:
        return out
    return [ev("C12_tax_id", "COMPANY", claim, "verified",
               f"{'GSTIN ' + gstin if gstin else 'PAN ' + pan} is well-formed ({entity or 'entity type unknown'}); "
               "live GST-portal check not connected", src)]
