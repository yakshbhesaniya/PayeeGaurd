"""CHANNEL family: is the message really from the vendor? C2, C2b, C2c, C8, C10 (spec 7, edge cases H1-H11)."""

import re
from datetime import datetime, timezone

from . import config, geo
from .checks_company import company_domains, identify
from .evidence import describe, ev, first_match, reason_for, unchecked


def _brand(vendor_name: str | None) -> str | None:
    if not vendor_name:
        return None
    words = re.findall(r"[a-z0-9]+", vendor_name.lower())
    stop = {"the", "inc", "ltd", "llc", "pvt", "private", "limited", "corp", "corporation", "co", "company", "gmbh"}
    words = [w for w in words if w not in stop]
    return words[0] if words and len(words[0]) >= 3 else None


def lookalike_reason(sender_host: str, real_domain: str) -> str | None:
    """Safe lookalike rule (spec 7). Returns why the sender host imitates real_domain, or None."""
    min_len = config.policy()["channel"]["lookalike_min_label_len"]
    s_reg = geo.registrable_domain(sender_host)
    if not s_reg or s_reg == real_domain:
        return None
    s_label, r_label = geo.label_of(s_reg), geo.label_of(real_domain)
    s_sk, r_sk = geo.skeleton(s_label), geo.skeleton(r_label)
    if s_label == r_label:
        return f"same name on a different TLD ({s_reg} vs {real_domain})"
    tokens = re.split(r"[-_.]+", s_sk)
    if len(r_sk) >= 3 and r_sk in tokens and s_sk != r_sk:
        return f"brand '{r_label}' plus extra words ({s_reg})"
    if len(r_label) >= min_len:
        if s_sk == r_sk:
            return f"homoglyph of {real_domain} ({s_reg})"
        limit = 1 if len(r_label) < 8 else 2
        if geo.levenshtein(s_sk, r_sk) <= limit:
            return f"one or two characters away from {real_domain} ({s_reg})"
    # Brand hidden in a subdomain of an unrelated domain (H3): freshworks.com.secure-pay.ru
    host_labels = geo.ascii_host(sender_host).split(".")[:-2]
    if any(geo.skeleton(lbl) == r_sk for lbl in host_labels) and len(r_sk) >= 3:
        return f"brand '{r_label}' used as a subdomain of {s_reg}"
    return None


def _brand_lookalike(sender_host: str, brand: str) -> str | None:
    s_reg = geo.registrable_domain(sender_host)
    if not s_reg or not brand:
        return None
    s_sk = geo.skeleton(geo.label_of(s_reg))
    tokens = re.split(r"[-_.]+", s_sk)
    if brand in tokens and s_sk != brand:
        return f"vendor name '{brand}' plus extra words ({s_reg})"
    if len(brand) >= 5 and s_sk != brand and geo.levenshtein(s_sk, brand) <= (1 if len(brand) < 8 else 2):
        return f"close spelling of the vendor name '{brand}' ({s_reg})"
    return None


def known_domains(ctx) -> set[str]:
    doms = company_domains(ctx.get("company"))
    doms |= set(ctx.get("history_domains") or set())
    return doms


def check_c2(inv, ctx):
    """C2 sender domain, C2b free mail, C2c display-name spoofing."""
    out = []
    claim = "Sender domain belongs to the vendor"
    pol = config.policy()["channel"]
    if not inv.sender_email:
        return [unchecked("C2_domain", "CHANNEL", claim, "No sender email in the request", "extract", "not_applicable")]
    host = inv.sender_email.rsplit("@", 1)[1]
    s_reg = geo.registrable_domain(host)
    cid = ctx.get("company_id")
    src = f"crustdata:company/{cid}#basic_info.all_domains" if cid else "graph:PAID_FROM_DOMAIN"
    real = known_domains(ctx)

    out += _display_name(inv, ctx, s_reg)
    if s_reg in pol["free_mail"]:
        out.append(ev("C2b_freemail", "CHANNEL", "Sender uses a company mailbox, not free mail", "contradicted",
                      f"{inv.sender_email} is a free mailbox ({s_reg})", f"policy:channel.free_mail#{s_reg}", 25))
        out.append(unchecked("C2_domain", "CHANNEL", claim, "Free-mail sender; domain match not applicable",
                             "extract", "not_applicable"))
        return out
    if s_reg in real:
        where = "vendor's domains on record" if s_reg in company_domains(ctx.get("company")) else "domain used in past paid invoices"
        out.append(ev("C2_domain", "CHANNEL", claim, "verified", f"{s_reg} is in the {where}", src, confirms="channel"))
        return out
    if s_reg in pol["trusted_processors"]:
        out.append(ev("C2_domain", "CHANNEL", claim, "verified", f"{s_reg} is a known payment processor",
                      f"policy:channel.trusted_processors#{s_reg}"))
        return out
    for d in sorted(real):
        why = lookalike_reason(host, d)
        if why:
            out.append(ev("C2_lookalike", "CHANNEL", "Sender domain is not a lookalike of the vendor's domain",
                          "contradicted", f"Lookalike: {why}", src, hard=True))
            return out
    # Vendor not resolved: compare with domains of a vendor we already KNOW by that name (graph). A website that
    # merely contains the vendor's own name is normal for an unknown vendor and is never treated as a lookalike.
    brand = _brand(inv.vendor_name)
    if brand and not real and ctx.get("graph") is not None:
        for d in sorted(ctx["graph"].known_domains_for_brand(geo.skeleton(brand))):
            why = lookalike_reason(host, d)
            if why:
                out.append(ev("C2_lookalike", "CHANNEL", "Sender domain is not a lookalike of the vendor's domain",
                              "contradicted", f"Lookalike of known vendor domain: {why}", f"graph:OWNS_DOMAIN/{d}",
                              hard=True))
                return out
    if not real:
        if cid is None:
            out.append(unchecked("C2_domain", "CHANNEL", claim, "Vendor's real domains unknown (vendor not resolved)",
                                 "crustdata:company/identify", ctx.get("c1_reason", "no_data")))
            return out
    # Sibling domain (H5): does the sender's own domain resolve to the same company?
    if cid is not None:
        try:
            best, _ = first_match(identify(ctx["client"], domain=s_reg))
            if best and best["company_data"]["crustdata_company_id"] == cid:
                out.append(ev("C2_domain", "CHANNEL", claim, "verified",
                              f"{s_reg} resolves to the same company (crustdata_company_id {cid})",
                              f"crustdata:company/identify?domains={s_reg}", confirms="channel"))
                return out
        except Exception:  # noqa: BLE001 - fall through to 'unrelated'
            pass
    out.append(ev("C2_unrelated", "CHANNEL", claim, "contradicted",
                  f"{s_reg} is not one of the vendor's domains ({', '.join(sorted(real)) or 'unknown'}); "
                  "could be a payment processor", src, 15))
    return out


def _display_name(inv, ctx, s_reg):
    """C2c (H4): a display name that contains a different email or domain than the real address."""
    name = ctx.get("display_name") or ""
    m = re.search(r"[\w.-]+@([\w-]+\.[\w.-]+)|\b([\w-]+\.(?:com|in|net|org|co|io))\b", name.lower())
    if not m:
        return []
    shown = geo.registrable_domain(m.group(1) or m.group(2))
    if shown and shown != s_reg:
        return [ev("C2c_display_name", "CHANNEL", "Display name matches the real sender address", "contradicted",
                   f"Display name shows {shown} but the email comes from {s_reg}", "extract:header#From", 30)]
    return []


def check_c10(inv, ctx):
    claim = "Replies go to the same organisation as the sender"
    others = [(h, e) for h, e in (("Reply-To", inv.reply_to_email), ("Return-Path", inv.return_path_email)) if e]
    if not others or not inv.sender_email:
        return [unchecked("C10_reply_to", "CHANNEL", claim, "No Reply-To / Return-Path header", "extract",
                          "not_applicable")]
    s_reg = geo.email_domain(inv.sender_email)
    real = known_domains(ctx) | {s_reg}
    out = []
    for header, email in others:
        d = geo.email_domain(email)
        if d in real:
            continue
        for r in sorted(known_domains(ctx)):
            why = lookalike_reason(email.rsplit("@", 1)[1], r)
            if why:
                out.append(ev("C10_reply_to_lookalike", "CHANNEL", claim, "contradicted",
                              f"{header} {email}: {why}", f"extract:header#{header}", hard=True))
                break
        else:
            out.append(ev("C10_reply_to", "CHANNEL", claim, "contradicted",
                          f"{header} sends replies to {d}, not {s_reg}", f"extract:header#{header}", 30))
    return out or [ev("C10_reply_to", "CHANNEL", claim, "verified", "Reply-To matches the sender's organisation",
                      "extract:header#Reply-To")]


def check_c8(inv, ctx):
    """Sender domain age (RDAP) + MX (DNS-over-HTTPS). Skipped for free mail and already-verified domains."""
    claim = "Sender domain is established"
    if not inv.sender_email:
        return [unchecked("C8_domain_age", "CHANNEL", claim, "No sender email", "extract", "not_applicable")]
    d = geo.email_domain(inv.sender_email)
    if d in config.policy()["channel"]["free_mail"] or d in known_domains(ctx):
        return [unchecked("C8_domain_age", "CHANNEL", claim, "Not needed for this domain", "extract", "not_applicable")]
    client = ctx["client"]
    try:
        dns = client.dns(d, "MX")
        rd_status, rd = client.http_get_cached(f"https://rdap.org/domain/{d}")
    except Exception as e:  # noqa: BLE001
        return [unchecked("C8_domain_age", "CHANNEL", claim, f"Domain lookup failed: {describe(e)}", f"rdap:{d}",
                          reason_for(e))]
    out = []
    nxdomain = isinstance(dns, dict) and dns.get("Status") == 3
    if nxdomain:
        return [ev("C8_domain_age", "CHANNEL", claim, "contradicted",
                   f"{d} does not exist in DNS: email cannot legitimately come from it", f"dns:{d}#MX", 25)]
    if isinstance(dns, dict) and dns.get("Status") == 0 and not dns.get("Answer"):
        out.append(ev("C8_mx", "CHANNEL", "Sender domain can receive email", "contradicted",
                      f"{d} has no MX record", f"dns:{d}#MX", 10))
    reg = None
    if rd_status == 200 and isinstance(rd, dict):
        for e in rd.get("events") or []:
            if e.get("eventAction") == "registration":
                reg = e.get("eventDate")
    if not reg:
        out.append(unchecked("C8_domain_age", "CHANNEL", claim, f"Registration date unavailable for {d} (RDAP {rd_status})",
                             f"rdap:{d}#events.registration", "no_data"))
        return out
    when = datetime.fromisoformat(reg.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - when).days
    if days < 30:
        out.append(ev("C8_domain_age", "CHANNEL", claim, "contradicted", f"{d} registered {days} days ago",
                      f"rdap:{d}#events.registration", 25))
    elif days < 90:
        out.append(ev("C8_domain_age", "CHANNEL", claim, "contradicted", f"{d} registered {days} days ago",
                      f"rdap:{d}#events.registration", 15))
    else:
        out.append(ev("C8_domain_age", "CHANNEL", claim, "verified", f"{d} registered {when.date()}",
                      f"rdap:{d}#events.registration"))
    return out
