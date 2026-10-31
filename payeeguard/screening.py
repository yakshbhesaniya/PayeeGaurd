"""Vendor-master screening: audit an entire vendor list in one batch (a paid audit service, replaced).

Per vendor: resolve in Crustdata, then look ACROSS the list and the payment graph for what single-invoice checks
can't see: one bank account shared by several vendors, duplicate vendor records for the same company, a vendor
domain that imitates another vendor, an account already used in a blocked attack, shrinking or unknown vendors.
"""

import csv
import io
from collections import defaultdict

from . import geo
from .checks_channel import lookalike_reason
from .checks_company import ENRICH_FIELDS, company_countries, identify
from .evidence import describe, first_match
from .graph import company_node

ENRICH_BATCH = 25   # Company Enrich accepts up to 25 ids per request


def parse_csv(text: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text.strip())):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if r.get("name") or r.get("domain"):
            rows.append(r)
    return rows


def screen(rows: list[dict], client, graph) -> dict:
    vendors = []
    for i, r in enumerate(rows):
        dom = geo.registrable_domain(r.get("domain", "").replace("https://", "").replace("http://", "").split("/")[0])
        fp = graph.fingerprint(r.get("iban") or None, r.get("account") or None,
                               (r.get("swift") or r.get("ifsc") or None))
        vendors.append({"row": i + 1, "name": r.get("name") or dom, "domain": dom, "fp": fp,
                        "country": geo.to_iso2(r.get("country")) or (fp.country if fp else None),
                        "cid": None, "company": None, "findings": [], "status": "OK"})

    # 1. Resolve every domain (Identify is free), then enrich the resolved ids in batches of 25.
    errors = []
    for v in vendors:
        if not v["domain"]:
            continue
        try:
            best, _ = first_match(identify(client, domain=v["domain"]))
            if best and (best.get("confidence_score") or 0) >= 0.8:
                v["cid"] = best["company_data"]["crustdata_company_id"]
                v["company"] = best["company_data"]
        except Exception as e:  # noqa: BLE001 - 400 for invalid domains, outages: vendor stays unresolved
            if "400" not in str(e):
                errors.append(f"{v['domain']}: {describe(e)}")
    ids = sorted({v["cid"] for v in vendors if v["cid"]})
    enriched = {}
    for k in range(0, len(ids), ENRICH_BATCH):
        try:
            resp = client.post("/company/enrich", {"crustdata_company_ids": ids[k:k + ENRICH_BATCH],
                                                   "fields": ENRICH_FIELDS})
            for item in resp or []:
                for m in item.get("matches") or []:
                    cd = m.get("company_data") or {}
                    enriched[cd.get("crustdata_company_id")] = cd
        except Exception as e:  # noqa: BLE001
            errors.append(f"enrich: {describe(e)}")
    for v in vendors:
        if v["cid"] in enriched:
            v["company"] = enriched[v["cid"]]

    def add(v, level, text, source):
        v["findings"].append({"level": level, "text": text, "source": source})
        rank = {"OK": 0, "REVIEW": 1, "ALERT": 2}
        if rank[level] > rank[v["status"]]:
            v["status"] = level

    # 2. Per-vendor facts.
    for v in vendors:
        cite = f"crustdata:company/{v['cid']}"
        if not v["domain"]:
            add(v, "REVIEW", "No website/domain on the vendor record", "vendor_master")
        elif not v["cid"]:
            add(v, "REVIEW", f"{v['domain']} not found in Crustdata: verify registration documents (not proof of fraud)",
                f"crustdata:company/identify?domains={v['domain']} -> no match")
        if v["company"]:
            info = v["company"].get("basic_info") or {}
            head = v["company"].get("headcount") or {}
            yoy = (head.get("growth_percent") or {}).get("yoy")
            if isinstance(yoy, (int, float)) and yoy < -30:
                add(v, "REVIEW", f"{info.get('name')} headcount fell {round(yoy, 1)}% in a year (distressed vendors are "
                    "common 'new bank details' targets)", f"{cite}#headcount.growth_percent.yoy")
            countries = company_countries(v["company"])
            if v["country"] and countries and v["country"] not in countries:
                add(v, "REVIEW", f"Bank country {v['country']} is not where {info.get('name')} operates "
                    f"({', '.join(sorted(countries))})", f"{cite}#locations")
        if v["fp"] and v["fp"].strong:
            for f in graph.cases_flagged(v["fp"].id):
                add(v, "ALERT", f"Bank account {v['fp'].masked} was used in blocked fraud case {f['case_id']}",
                    f"graph:FLAGGED_IN/{v['fp'].id}->case:{f['case_id']}")
                break

    # 3. Cross-vendor patterns (the part per-invoice checks can't see).
    by_acct, by_cid = defaultdict(list), defaultdict(list)
    for v in vendors:
        if v["fp"] and v["fp"].strong:
            by_acct[v["fp"].id].append(v)
        if v["cid"]:
            by_cid[v["cid"]].append(v)
    for acct, group in by_acct.items():
        if len({g["cid"] or g["domain"] for g in group}) > 1:
            names = ", ".join(f"{g['name']} (row {g['row']})" for g in group)
            for g in group:
                add(g, "ALERT", f"Same bank account {g['fp'].masked} on {len(group)} different vendors: {names}. "
                    "Possible mule, factoring company or insider-created vendor", f"vendor_master:{acct}")
    for cid, group in by_cid.items():
        if len(group) > 1:
            names = ", ".join(f"{g['name']} (row {g['row']})" for g in group)
            for g in group:
                add(g, "REVIEW", f"Duplicate vendor record: {names} are the same company (crustdata_company_id {cid}); "
                    "duplicate records enable double payments", f"crustdata:company/{cid}")
    real = {v["domain"]: v for v in vendors if v["cid"] and v["domain"]}
    known = {d for d in real} | {d for v in vendors if v["cid"] for d in
                                 [geo.registrable_domain((v["company"].get("basic_info") or {}).get("primary_domain"))] if d}
    for v in vendors:
        if not v["domain"] or v["domain"] in known:
            continue
        for d in sorted(known):
            why = lookalike_reason(v["domain"], d)
            if why:
                add(v, "ALERT", f"Vendor domain imitates another vendor's real domain: {why}", f"vendor_master:{d}")
                break
        if v["status"] != "ALERT" and graph is not None:
            label = geo.skeleton(geo.label_of(v["domain"]).split("-")[0])
            for d in sorted(graph.known_domains_for_brand(label)):
                why = lookalike_reason(v["domain"], d)
                if why and d != v["domain"]:
                    add(v, "ALERT", f"Vendor domain imitates a vendor you pay: {why}", f"graph:OWNS_DOMAIN/{d}")
                    break

    for v in vendors:
        if not v["findings"]:
            info = (v["company"] or {}).get("basic_info") or {}
            head = (v["company"] or {}).get("headcount") or {}
            v["findings"].append({"level": "OK", "text": f"Verified: {info.get('name')}, founded "
                                  f"{info.get('year_founded') or '?'}, {head.get('total', '?')} staff",
                                  "source": f"crustdata:company/{v['cid']}#basic_info"})
    counts = defaultdict(int)
    for v in vendors:
        counts[v["status"]] += 1
    out = [{"row": v["row"], "name": v["name"], "domain": v["domain"], "status": v["status"],
            "company_id": v["cid"], "account": v["fp"].masked if v["fp"] else None,
            "node": company_node(v["cid"]) if v["cid"] else None, "findings": v["findings"]} for v in vendors]
    return {"vendors": out, "counts": dict(counts), "errors": errors, "total": len(vendors),
            "credits_estimate": 2 * len(ids)}
