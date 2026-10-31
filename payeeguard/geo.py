"""Country, domain and bank-identifier helpers."""

import re
import unicodedata

import idna

COUNTRY_NAMES = {
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US", "america": "US",
    "india": "IN", "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB", "britain": "GB",
    "nigeria": "NG", "germany": "DE", "france": "FR", "singapore": "SG", "united arab emirates": "AE",
    "uae": "AE", "canada": "CA", "australia": "AU", "netherlands": "NL", "ireland": "IE", "japan": "JP",
    "china": "CN", "brazil": "BR", "spain": "ES", "italy": "IT", "switzerland": "CH", "sweden": "SE",
    "israel": "IL", "hong kong": "HK", "mexico": "MX", "south africa": "ZA", "belgium": "BE",
    "poland": "PL", "portugal": "PT", "new zealand": "NZ", "philippines": "PH", "indonesia": "ID",
    "malaysia": "MY", "thailand": "TH", "vietnam": "VN", "kenya": "KE", "ghana": "GH", "pakistan": "PK",
    "bangladesh": "BD", "sri lanka": "LK", "nepal": "NP", "russia": "RU", "turkey": "TR", "egypt": "EG",
    "saudi arabia": "SA", "qatar": "QA", "denmark": "DK", "norway": "NO", "finland": "FI", "austria": "AT",
}
ISO2 = set(COUNTRY_NAMES.values())

MULTI_PART_SUFFIXES = {
    "co.in", "net.in", "org.in", "ac.in", "gov.in", "firm.in", "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au", "co.jp", "com.sg", "com.br", "co.nz", "com.mx", "co.za", "com.cn",
    "com.hk", "com.my", "co.id", "com.tr", "com.sa", "co.ke", "com.ng",
}

# Expected IBAN lengths, used to trim trailing text after an IBAN.
IBAN_LENGTHS = {
    "GB": 22, "DE": 22, "FR": 27, "NL": 18, "IE": 22, "ES": 24, "IT": 27, "CH": 21, "AE": 23, "BE": 16,
    "AT": 20, "PT": 25, "PL": 28, "SE": 24, "DK": 18, "NO": 15, "FI": 18, "LU": 20, "SA": 24, "QA": 29,
    "TR": 26, "IL": 23,
}

# Characters attackers swap in: ASCII look-alikes and common Cyrillic/Greek homoglyphs.
HOMOGLYPHS = str.maketrans({
    "0": "o", "1": "l", "3": "e", "5": "s", "ı": "i",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ѕ": "s", "ԁ": "d",
    "ӏ": "l", "ј": "j", "ο": "o", "α": "a", "ν": "v",
})


def to_iso2(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.upper() in ISO2:
        return v.upper()
    return COUNTRY_NAMES.get(v.lower().strip(" .()"))


def office_country(address: str) -> str | None:
    """Crustdata office addresses end with an ISO-2 code: '79 Hatton Garden, London, ..., GB'."""
    last = address.rsplit(",", 1)[-1].strip()
    return to_iso2(last)


def find_country(text: str) -> str | None:
    """First country name mentioned in text (longest names first so 'United States' beats 'US')."""
    low = " " + text.lower() + " "
    best = None
    for name in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if len(name) <= 3:
            m = re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text)  # short codes: case-sensitive upper
            if m is None:
                m = re.search(rf"(?<![A-Za-z]){re.escape(name.upper())}(?![A-Za-z])", text)
        else:
            m = re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), COUNTRY_NAMES[name])
    return best[1] if best else None


def ascii_host(host: str) -> str:
    """Lowercase, strip trailing dot, decode punycode to Unicode where possible (for homoglyph checks)."""
    h = host.strip().strip(".").lower()
    labels = []
    for label in h.split("."):
        if label.startswith("xn--"):
            try:
                label = idna.decode(label)
            except idna.IDNAError:
                pass
        labels.append(label)
    return ".".join(labels)


def registrable_domain(host: str | None) -> str | None:
    if not host:
        return None
    h = ascii_host(host)
    parts = [p for p in h.split(".") if p]
    if len(parts) < 2:
        return None
    if ".".join(parts[-2:]) in MULTI_PART_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def label_of(domain: str) -> str:
    """'freshworks.com' -> 'freshworks'; 'acme.co.in' -> 'acme'."""
    parts = domain.split(".")
    if ".".join(parts[-2:]) in MULTI_PART_SUFFIXES and len(parts) >= 3:
        return parts[-3]
    return parts[0] if len(parts) == 2 else parts[-2]


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return registrable_domain(email.rsplit("@", 1)[1])


def skeleton(text: str) -> str:
    """Homoglyph-normalised form: NFKC, map look-alikes, rn->m, vv->w."""
    t = unicodedata.normalize("NFKC", text.lower()).translate(HOMOGLYPHS)
    return t.replace("rn", "m").replace("vv", "w")


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def iban_normalise(iban: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", iban).upper()


def iban_valid(iban: str) -> bool:
    s = iban_normalise(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s):
        return False
    if s[:2] in IBAN_LENGTHS and len(s) != IBAN_LENGTHS[s[:2]]:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def swift_valid(swift: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?", swift.upper()))


def swift_country(swift: str | None) -> str | None:
    return swift[4:6].upper() if swift and len(swift) >= 6 else None


def ifsc_valid(ifsc: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc.upper()))


def is_ifsc(code: str | None) -> bool:
    return bool(code) and ifsc_valid(code)


# ---- India GST / PAN ------------------------------------------------------------------------------------
GST_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}
PAN_ENTITY = {"C": "company", "P": "individual", "F": "firm/LLP", "H": "HUF", "A": "association", "T": "trust",
              "B": "body of individuals", "L": "local authority", "J": "artificial juridical person", "G": "government"}
_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b"


def gstin_check_char(first14: str) -> str:
    """GSTIN checksum (mod-36 Luhn variant) over the first 14 characters."""
    total = 0
    for i, ch in enumerate(first14.upper()):
        prod = _GST_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return _GST_CHARS[(36 - total % 36) % 36]


def gstin_valid(gstin: str) -> bool:
    g = gstin.strip().upper()
    if not re.fullmatch(GSTIN_RE[2:-2], g) or g[:2] not in GST_STATES:
        return False
    return gstin_check_char(g[:14]) == g[14]


def office_state_in(address: str) -> str | None:
    """'..., Chennai, Tamil Nadu, 600096, IN' -> 'Tamil Nadu' (Indian offices only)."""
    parts = [p.strip() for p in address.split(",")]
    if not parts or parts[-1].upper() != "IN":
        return None
    for p in reversed(parts[:-1]):
        if p in GST_STATES.values():
            return p
    return None
